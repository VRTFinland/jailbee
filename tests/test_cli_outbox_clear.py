"""Clear command contracts through the real root CLI and deletion service."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.cli_outbox import normalize_outbox_argv
from jailbee.incus import Incus
from jailbee.outbox import service
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, container_prefix="acme")
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.config.load_config", return_value=cfg)
    incus = mocker.Mock(spec=Incus)
    raw = {
        "name": IDENTITY.full_name,
        "created_at": IDENTITY.created_at,
        "profiles": ["acme-base"],
        "status": "Running",
    }
    incus.list_containers.return_value = [raw]
    incus.exists.side_effect = lambda name: name in {IDENTITY.full_name, "acme-other"}
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    snapshots = {"pr": store("pr", pr_files()), "issue": store("issue", issue_files())}
    mocker.patch.object(service, "read_store", side_effect=lambda i, c, k, **kw: snapshots[k])
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    mocker.patch("jailbee.outbox_io.JournalStore", return_value=JournalStore(tmp_path / "journals"))
    return incus, mutation, raw, snapshots


def test_clear_help_lists_options():
    result = CliRunner().invoke(app, ["outbox", "clear", "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output
    assert "--yes" in result.output


def test_clear_off_tty_without_container_exits_two(env, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = CliRunner().invoke(app, ["outbox", "clear"])
    assert result.exit_code == 2, result.output
    assert "missing container" in result.output
    assert IDENTITY.full_name in result.output
    env[1].assert_not_called()


def test_clear_on_tty_asks_even_for_one_container(env, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", return_value=IDENTITY.full_name)
    result = CliRunner().invoke(app, ["outbox", "clear", "-y"])
    assert result.exit_code == 0, result.output
    select.assert_called_once()
    assert env[1].call_count == 2


def test_clear_yes_skips_question(env, mocker):
    confirm = mocker.patch("typer.confirm", side_effect=AssertionError("unexpected prompt"))
    result = CliRunner().invoke(app, ["outbox", "clear", "feature", "-y"])
    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert env[1].call_count == 2


@pytest.mark.parametrize(
    "names, singular, question",
    [
        (["feature"], False, "Delete 2 proposals in 1 container?"),
        (["feature"], True, "Delete 1 proposal in 1 container?"),
        (["feature", "other"], False, "Delete 4 proposals in 2 containers?"),
    ],
)
def test_clear_declined_question_exits_zero(env, mocker, names, singular, question):
    incus, _, raw, snapshots = env
    incus.list_containers.return_value = [raw, {**raw, "name": "acme-other"}]
    if singular:
        snapshots["pr"] = store("pr", {})
    confirm = mocker.patch("typer.confirm", return_value=False)
    result = CliRunner().invoke(app, ["outbox", "clear", *names])
    assert result.exit_code == 0, result.output
    confirm.assert_called_once_with(question, default=False)
    assert "Nothing deleted." in result.output
    env[1].assert_not_called()


def test_missing_first_target_exits_one_and_still_clears_healthy(env):
    result = CliRunner().invoke(app, ["outbox", "clear", "vanished", "feature", "-y"])
    assert result.exit_code == 1, result.output
    assert "vanished: unavailable" in result.output
    assert env[1].call_count == 2
    assert {c.args[1] for c in env[1].call_args_list} == {"acme-feature"}


def test_clear_several_positionals(env, mocker):
    incus, mutation, raw, _ = env
    incus.list_containers.return_value = [raw, {**raw, "name": "acme-other"}]
    confirm = mocker.patch("typer.confirm", side_effect=AssertionError("unexpected prompt"))
    result = CliRunner().invoke(app, ["outbox", "clear", "feature", "other", "-y"])
    assert result.exit_code == 0, result.output
    assert mutation.call_count == 4
    assert {call.args[1] for call in mutation.call_args_list} == {"acme-feature", "acme-other"}
    confirm.assert_not_called()


def test_clear_dry_run_never_confirms_or_deletes(env, mocker):
    confirm = mocker.patch("typer.confirm", side_effect=AssertionError("unexpected prompt"))
    result = CliRunner().invoke(app, ["outbox", "clear", "feature", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "issue/001.json" in result.output and "pr/001.json" in result.output
    confirm.assert_not_called()
    env[1].assert_not_called()


def test_clear_question_counts_only_delete_containers(env, mocker):
    incus, mutation, raw, snapshots = env
    incus.list_containers.return_value = [raw, {**raw, "name": "acme-other"}]
    mocker.patch.object(service, "read_store", side_effect=lambda i, c, k, **kw: (
        store(k, pr_files() | {"applied.log": "now 001.json pr=42 actions=1 urls=x"})
        if c == "acme-other" and k == "pr" else
        store(k, {}) if c == "acme-other" else snapshots[k]
    ))
    confirm = mocker.patch("typer.confirm", return_value=False)
    result = CliRunner().invoke(app, ["outbox", "clear", "feature", "other", "missing"])
    assert result.exit_code == 0, result.output
    confirm.assert_called_once_with("Delete 2 proposals in 1 container?", default=False)
    mutation.assert_not_called()


def test_snapshot_prepare_and_clear_cli_bind_before_question(env, mocker):
    import json

    from jailbee.outbox.clear_snapshot import decode_snapshot

    confirm = mocker.patch("typer.confirm", side_effect=AssertionError("second prompt"))
    prepared = CliRunner().invoke(app, ["outbox", "clear", "acme-feature", "--prepare-snapshot"])
    assert prepared.exit_code == 0, prepared.output
    token = prepared.stdout.strip()
    assert len(decode_snapshot(token)[0].delete) == 2
    env[1].assert_not_called()
    files = pr_files()
    files["001.json"] += "\n"
    env[3]["pr"] = store("pr", files)
    env[3]["issue"] = store("issue", issue_files() | {"002.json": issue_files()["001.json"]})
    result = CliRunner().invoke(app, ["outbox", "clear", "acme-feature", "-y", "--approved-snapshot", token])
    assert result.exit_code == 1, result.output
    assert env[1].call_count == 1
    assert env[1].call_args.kwargs["delete_names"] == ("001.json",)
    confirm.assert_not_called()
    assert json.loads(token)["version"] == 1


@pytest.mark.parametrize("token", ['{}', '/etc/passwd', '{"version":true,"plans":[]}', '{"version":1,"plans":[]}'])
def test_invalid_snapshot_is_usage_error_without_deletion(env, token):
    result = CliRunner().invoke(app, ["outbox", "clear", "acme-feature", "-y", "--approved-snapshot", token])
    assert result.exit_code == 2, result.output
    env[1].assert_not_called()


@pytest.mark.parametrize("names", [["acme-other"], ["feature"], ["acme-feature", "acme-other"]])
def test_snapshot_cannot_address_another_or_implicit_container(env, names):
    prepared = CliRunner().invoke(app, ["outbox", "clear", "acme-feature", "--prepare-snapshot"])
    result = CliRunner().invoke(app, ["outbox", "clear", *names, "-y", "--approved-snapshot", prepared.stdout.strip()])
    assert result.exit_code == 2, result.output
    env[1].assert_not_called()


@pytest.mark.parametrize("options", [
    ["--prepare-snapshot", "-y"], ["--prepare-snapshot", "--dry-run"],
    ["--prepare-snapshot", "--approved-snapshot", "{}"],
])
def test_snapshot_preparation_rejects_execution_options(env, options):
    result = CliRunner().invoke(app, ["outbox", "clear", "acme-feature", *options])
    assert result.exit_code == 2, result.output
    env[1].assert_not_called()


def test_dashboard_question_to_real_clear_child_preserves_snapshot(env, mocker, tmp_path):
    from jailbee.dashboard.jobs import JobResult
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.overlays import Picker
    from jailbee.git_status import GitStatus
    from tests.dashboard_fixtures import ci
    from tests.dashboard_pilot import bare_session

    info = ci("acme-feature", "acme", "Running")
    info.git_status = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok", pending_pr_actions=1, pending_issue_actions=1,
    )
    session, terminal = bare_session(mocker, [RepoGroup("acme", str(tmp_path), None, [info])])
    children = []

    def start(key, label, argv, cwd, on_done, **kwargs):
        children.append(argv)
        result = CliRunner().invoke(app, argv[1:])
        session.jobs._labels[key] = label
        session.jobs._finished.append((key, on_done, JobResult(result.exit_code, result.stderr, result.stdout)))

    mocker.patch.object(session.jobs, "start", side_effect=start)
    session.marked = frozenset({"acme-feature"})
    session.overlay = session.begin_bulk("outbox clear")
    assert len(children) == 1
    env[1].assert_not_called()
    session.tick()
    assert isinstance(session.overlay, Picker)
    assert session.overlay.purpose == "bulk-outbox-clear-confirm"
    files = pr_files()
    files["001.json"] += "\n"
    env[3]["pr"] = store("pr", files)
    env[3]["issue"] = store("issue", issue_files() | {"002.json": issue_files()["001.json"]})
    confirm = mocker.patch("typer.confirm", side_effect=AssertionError("second question"))
    session.picker_chosen(session.overlay.entries[1])
    session.tick()
    assert len(children) == 2
    assert env[1].call_count == 1
    assert env[1].call_args.args[1:3] == ("acme-feature", "issue")
    assert env[1].call_args.kwargs["delete_names"] == ("001.json",)
    assert "kept 1" in session.notice
    assert terminal.handed == []
    confirm.assert_not_called()


def test_normalize_keeps_clear():
    argv = ["outbox", "clear", "x", "-y"]
    assert normalize_outbox_argv(argv) == argv
