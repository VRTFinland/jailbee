"""Scripted terminal navigation using real inspection, deletion and CLI boundaries."""

from dataclasses import replace

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.outbox.delete import DeleteSelection
from jailbee.outbox.inspect import build_views
from jailbee.outbox.models import ContainerView, OutboxChanged
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, pr_files, store
from tests.test_cli_outbox import env as cli_env
from tests.test_cli_outbox import publication_env as cli_publication_env

env = cli_env
publication_env = cli_publication_env


@pytest.fixture
def view(tmp_path):
    stores = (store("pr", pr_files()), store("issue", issue_files()))
    proposals = build_views(IDENTITY, stores, journal_store=JournalStore(tmp_path / "journal"))
    return ContainerView(IDENTITY, IDENTITY.full_name, True, None, stores, proposals)


def drive(view, choices, *, delete=None, publish=None, confirm=None, load=None):
    from jailbee.outbox.browser import BrowserActions, run_browser

    answers = iter(choices)
    menus, output, confirmations, loads = [], [], [], []

    def choose(message, options):
        menus.append(tuple(options))
        answer = next(answers)
        if answer is not None:
            assert answer in dict(options), (answer, options)
        return answer

    def loading(name):
        loads.append(name)
        return load(name) if load else (view,)

    def confirming(message):
        confirmations.append(message)
        return confirm(message) if confirm else True

    assert (
        run_browser(BrowserActions(loading, delete, publish, confirming, choose, output.append))
        == 0
    )
    return menus, output, confirmations, loads


def test_selected_comment_delete_uses_original_revision(view):
    captured = []
    result = drive(
        view,
        [
            "container:acme-feature",
            "proposal:pr/001.json",
            "action:0",
            "comment:1",
            "delete",
            "exit",
        ],
        delete=lambda name, plan: captured.append(plan) or (),
    )
    assert captured[0].selection == DeleteSelection(action=0, comment=1)
    assert captured[0].expected_revision == view.proposals[0].revision
    assert result[3] == [None, None]
    assert "Second" in "\n".join(result[1])


def test_publish_from_child_is_whole_manifest(view):
    captured = []
    _, _, confirmations, _ = drive(
        view,
        [
            "container:acme-feature",
            "proposal:pr/001.json",
            "action:0",
            "comment:1",
            "publish",
            "exit",
        ],
        publish=lambda *args: captured.append(args) or 0,
    )
    assert captured == [(view.name, view.proposals[0].id, view.proposals[0].revision)]
    assert "all pending actions" in confirmations[0]


def test_cascade_scope_is_explicit_and_can_be_declined(view):
    captured = []
    _, _, confirmations, _ = drive(
        view,
        ["container:acme-feature", "proposal:issue/001.json", "action:0", "delete", "exit"],
        delete=lambda name, plan: captured.append(plan) or (),
    )
    assert captured[0].selection.with_dependents
    assert captured[0].removed_actions == (0, 1)
    assert "(0, 1)" in confirmations[-1]
    assert "dependent" in confirmations[0]
    captured.clear()
    drive(
        view,
        ["container:acme-feature", "proposal:issue/001.json", "action:0", "delete", "exit"],
        delete=lambda name, plan: captured.append(plan) or (),
        confirm=lambda _: False,
    )
    assert not captured


@pytest.mark.parametrize("changed", [False, True])
def test_refresh_retains_child_only_for_unchanged_revision(view, changed):
    new = (
        replace(view, proposals=(replace(view.proposals[0], revision="new"), *view.proposals[1:]))
        if changed
        else view
    )
    reads = iter([view, new])
    menus, _, _, _ = drive(
        view,
        [
            "container:acme-feature",
            "proposal:pr/001.json",
            "action:0",
            "comment:1",
            "refresh",
            "exit",
        ],
        load=lambda _: (next(reads),),
    )
    assert ("back", "Back") in menus[-1]
    assert any(key == "action:0" for key, _ in menus[-1]) == changed


def test_back_refresh_cancel_and_no_mutation_callbacks(view):
    menus, _, _, loads = drive(
        view,
        [
            "container:acme-feature",
            "proposal:pr/001.json",
            "action:0",
            "comment:1",
            "back",
            "back",
            "back",
            "back",
            "refresh",
            None,
        ],
    )
    assert len(loads) == 2
    assert not any(key in ("delete", "publish") for menu in menus for key, _ in menu)


@pytest.mark.parametrize("state", ["empty", "unavailable", "invalid"])
def test_empty_unavailable_invalid_details(view, state):
    if state == "empty":
        view = replace(view, proposals=())
    elif state == "unavailable":
        view = replace(view, available=False, error="read failed", proposals=())
    else:
        view = replace(
            view,
            proposals=(
                replace(view.proposals[0], actions=(), error="invalid JSON", state="invalid"),
            ),
        )
    choices = (
        ["container:acme-feature"]
        + (["proposal:pr/001.json"] if state == "invalid" else [])
        + ["exit"]
    )
    _, output, _, _ = drive(view, choices)
    assert {"empty": "No proposals", "unavailable": "read failed", "invalid": "invalid JSON"}[
        state
    ] in "\n".join(output)


def test_mutation_error_refreshes_and_invalidates_child(view):
    new = replace(
        view, proposals=(replace(view.proposals[0], revision="changed"), *view.proposals[1:])
    )
    reads = iter([view, new])

    def fail(*args):
        raise OutboxChanged("refresh required")

    menus, output, _, _ = drive(
        view,
        ["container:acme-feature", "proposal:pr/001.json", "action:0", "delete", "exit"],
        delete=fail,
        load=lambda _: (next(reads),),
    )
    assert "refresh required" in output
    assert any(key == "action:0" for key, _ in menus[-1])


def test_safe_complete_details(view):
    view = replace(
        view,
        proposals=(
            replace(
                view.proposals[0],
                raw_text="[red]raw\x1b[2J",
                actions=(replace(view.proposals[0].actions[0], body="long body\n" * 300),),
            ),
        ),
    )
    _, output, _, _ = drive(view, ["container:acme-feature", "proposal:pr/001.json", "exit"])
    assert "long body\n" * 300 in "\n".join(output)
    assert "\x1b" not in "\n".join(output)
    assert "[red]raw" in "\n".join(output)


def test_cli_tty_real_delete(env, mocker):
    mocker.patch("typer.testing._NamedTextIOWrapper.isatty", return_value=True)
    mocker.patch("jailbee.cli_outbox.browser_read_only", return_value=False)
    answers = iter(
        [
            "container:acme-feature",
            "proposal:pr/001.json",
            "action:0",
            "comment:1",
            "delete",
            "exit",
        ]
    )
    mocker.patch(
        "questionary.select", side_effect=lambda *a, **kw: mocker.Mock(ask=lambda: next(answers))
    )
    mocker.patch("typer.confirm", return_value=True)
    result = CliRunner().invoke(app, ["outbox"])
    assert result.exit_code == 0, result.output
    payload = env[4].call_args.kwargs["new_manifest"][1]
    assert "First" in payload and "Second" not in payload


@pytest.mark.parametrize("ssh", [True, False])
def test_cli_read_only_and_off_tty_never_mutate(env, mocker, ssh):
    mocker.patch("typer.testing._NamedTextIOWrapper.isatty", return_value=ssh)
    mocker.patch("jailbee.cli_outbox.browser_read_only", return_value=ssh)
    answers = iter(["container:acme-feature", "proposal:pr/001.json", "exit"])
    menus = []

    def select(*a, **kw):
        menus.append([c.value for c in kw["choices"]])
        return mocker.Mock(ask=lambda: next(answers))

    mocker.patch("questionary.select", side_effect=select)
    mocker.patch("typer.confirm", side_effect=AssertionError("must not confirm"))
    result = CliRunner().invoke(app, ["outbox"])
    assert result.exit_code == 0, result.output
    assert bool(menus) == ssh
    assert not any(value in ("delete", "publish") for menu in menus for value in menu)
    env[4].assert_not_called()


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_cli_browser_real_publication(publication_env, mocker, kind):
    env, create, comment, review = publication_env
    mocker.patch("typer.testing._NamedTextIOWrapper.isatty", return_value=True)
    mocker.patch("jailbee.cli_outbox.browser_read_only", return_value=False)
    answers = iter(
        ["container:acme-feature", f"proposal:{kind}/001.json", "action:0", "publish", "exit"]
    )
    mocker.patch(
        "questionary.select", side_effect=lambda *a, **kw: mocker.Mock(ask=lambda: next(answers))
    )
    mocker.patch("typer.confirm", return_value=True)
    result = CliRunner().invoke(app, ["outbox"])
    assert result.exit_code == 0, result.output
    if kind == "pr":
        review.assert_called_once()
    else:
        create.assert_called_once()
        assert comment.call_count == 2
    assert "001.json" not in env[2][kind].as_dict()
    assert "002.json" in env[2][kind].as_dict()


def test_cli_delete_scope_change_after_consent_refuses(env, mocker):
    mocker.patch("typer.testing._NamedTextIOWrapper.isatty", return_value=True)
    mocker.patch("jailbee.cli_outbox.browser_read_only", return_value=False)
    answers = iter(["container:acme-feature", "proposal:pr/001.json", "delete", "exit"])
    mocker.patch(
        "questionary.select", side_effect=lambda *a, **kw: mocker.Mock(ask=lambda: next(answers))
    )

    def confirm(*args, **kwargs):
        from jailbee.remote_ssh.repo_scope import RemoteRepoScope

        mocker.patch(
            "jailbee.remote_ssh.repo_scope.scope_for_session",
            return_value=RemoteRepoScope(frozenset({"acme"})),
        )
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox"])
    assert result.exit_code == 0, result.output
    env[4].assert_not_called()
    assert "no such container" in result.output
