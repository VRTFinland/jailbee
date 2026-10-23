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


def test_normalize_keeps_clear():
    argv = ["outbox", "clear", "x", "-y"]
    assert normalize_outbox_argv(argv) == argv
