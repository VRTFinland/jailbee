"""Unified outbox contracts exercised through the real root Typer app."""

import json

import pytest
import typer
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.incus import Incus
from jailbee.outbox import service
from jailbee.outbox.models import ContainerView, ProposalId
from jailbee.outbox_io import JournalStore
from tests.conftest import panel_text
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
    incus.exists.side_effect = lambda name: name == IDENTITY.full_name
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    snapshots = {"pr": store("pr", pr_files()), "issue": store("issue", issue_files())}
    reader = mocker.patch.object(
        service, "read_store", side_effect=lambda i, c, k, **kw: snapshots[k]
    )
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    journals = JournalStore(tmp_path / "journals")
    mocker.patch("jailbee.outbox_io.JournalStore", return_value=journals)
    return cfg, incus, snapshots, reader, mutation, journals, raw


@pytest.mark.parametrize("args", [["ls"], ["show", "feature", "issue/001.json"], ["browse"]])
def test_inspection_missing_registry_does_not_bootstrap_state(env, monkeypatch, tmp_path, args):
    state = tmp_path / "fresh-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 0, result.output
    assert not state.exists()


@pytest.mark.parametrize("leaf", [None, "browse", "ls", "show", "drop", "apply"])
def test_public_help(leaf):
    args = ["outbox"] + ([leaf] if leaf else []) + ["--help"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    if leaf is None:
        assert all(name in result.output for name in ("browse", "ls", "show", "drop", "apply"))
    if leaf == "drop":
        assert "zero-based" in result.output
        assert "--revision" in result.output


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["outbox"], ["outbox", "browse"]),
        (["outbox", "feature"], ["outbox", "browse", "feature"]),
        (["outbox", "ls", "feature"], ["outbox", "ls", "feature"]),
        (["outbox", "--help"], ["outbox", "--help"]),
        (
            ["outbox", "--config", "ls", "feature"],
            ["outbox", "--config", "ls", "browse", "feature"],
        ),
        (["outbox", "--config=x", "ls"], ["outbox", "--config=x", "ls"]),
        (["--version"], ["--version"]),
    ],
)
def test_normalize(argv, expected):
    from jailbee.cli_outbox import normalize_outbox_argv

    assert normalize_outbox_argv(argv) == expected


@pytest.mark.parametrize("args", [[], ["feature"], ["browse", "feature"]])
def test_browser_overview_without_prompt(env, mocker, args):
    prompt = mocker.patch("typer.confirm", side_effect=AssertionError("inspection prompted"))
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 0, result.output
    assert "issue/001.json" in result.output and "pr/001.json" in result.output
    prompt.assert_not_called()


@pytest.mark.parametrize("option", ["--format", "--output", "-o"])
@pytest.mark.parametrize("leaf", ["ls", "show"])
def test_json_aliases(env, option, leaf):
    args = ["outbox", leaf, "feature"] + (["issue/001.json"] if leaf == "show" else [])
    result = CliRunner().invoke(app, [*args, option, "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["schema"] == 1
    if leaf == "show":
        assert data["proposal"]["actions"][0]["index"] == 0
    else:
        assert data["containers"][0]["name"] == IDENTITY.full_name


@pytest.mark.parametrize(
    "args",
    [
        ["--config", "fixture.yaml", "ls"],
        ["ls", "--config", "fixture.yaml"],
        ["--config=fixture.yaml", "feature"],
    ],
)
def test_group_and_leaf_config_placement(env, mocker, args):
    loader = mocker.patch("jailbee.config.load_config", return_value=env[0])
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 0, result.output
    assert loader.call_args.args[0].name == "fixture.yaml"


@pytest.mark.parametrize(
    "args",
    [
        ["ls", "-o", "xml"],
        ["show", "feature", "issue/001.json", "-o", "xml"],
        ["drop", "feature", "../001.json", "-y"],
        ["apply", "feature", "issue/001.json", "--force", "-y"],
        ["drop", "feature", "issue/001.json", "--comment", "0", "-y"],
        ["drop", "feature", "issue/001.json", "--action", "0", "-y"],
        ["ls", "feature", "--all-repos"],
    ],
)
def test_validation_never_mutates(env, args):
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 2, result.output
    env[4].assert_not_called()
    env[1].start.assert_not_called()


@pytest.mark.parametrize("leaf", ["drop", "apply"])
def test_stale_revision_is_validation_error(env, leaf):
    result = CliRunner().invoke(
        app, ["outbox", leaf, "feature", "issue/001.json", "--revision", "stale", "-y"]
    )
    assert result.exit_code == 2, result.output
    assert "refresh" in result.output
    env[4].assert_not_called()


def test_drop_cancel_and_exact_confirmed_cascade(env):
    args = ["outbox", "drop", "feature", "issue/001.json", "--action", "0", "--with-dependents"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert "(0, 1)" in result.output
    env[4].assert_not_called()
    result = CliRunner().invoke(app, [*args, "-y"])
    assert result.exit_code == 0, result.output
    payload = json.loads(env[4].call_args.kwargs["new_manifest"][1])
    assert payload["actions"] == [
        {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"}
    ]


def test_drop_execution_error_maps_to_one(env):
    from jailbee.outbox.models import OutboxExecutionError

    env[4].side_effect = OutboxExecutionError("write failed")
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "issue/001.json", "-y"])
    assert result.exit_code == 1, result.output
    assert "write failed" in result.output


@pytest.mark.parametrize("name", [[], ["feature"]])
def test_stopped_is_structured_unavailable(env, name):
    env[6]["status"] = "Stopped"
    result = CliRunner().invoke(app, ["outbox", "ls", *name, "-o", "json"])
    assert result.exit_code == 2, result.output
    row = json.loads(result.stdout)["containers"][0]
    assert row["available"] is False and "stopped" in row["error"].lower()
    env[3].assert_not_called()


def test_literal_untrusted_detail(env):
    files = issue_files()
    files["body.md"] = "[bold]literal[/bold]\x1b\r‮ tail"
    env[2]["issue"] = store("issue", files)
    result = CliRunner().invoke(app, ["outbox", "show", "feature", "issue/001.json"])
    assert result.exit_code == 0, result.output
    assert "[bold]literal[/bold]" in result.output
    assert "\x1b" not in result.output and "‮" not in result.output


def test_typo_is_missing_container_not_silent_overview(env):
    result = CliRunner().invoke(app, ["outbox", "lss"])
    assert result.exit_code == 2
    assert "no such container" in result.output
    env[3].assert_not_called()


@pytest.fixture
def publication_env(env, mocker, tmp_path):
    import subprocess

    from jailbee import issue_github, issue_outbox, pr
    from jailbee.outbox import io
    from jailbee.outbox.io import PrManagement

    run_shell = subprocess.run
    directory = tmp_path / "mutation-outbox"
    directory.mkdir()

    def sync(kind):
        snapshot = env[2][kind]
        for path in directory.iterdir():
            path.unlink()
        for name, content in snapshot.files:
            (directory / name).write_bytes(content.encode("utf-8"))
        for name in snapshot.rejected:
            (directory / name).write_bytes(b"\xff")

    def save(kind):
        snapshot = env[2][kind]
        files = {
            p.name: p.read_bytes().decode("utf-8")
            for p in directory.iterdir()
            if p.name not in snapshot.rejected
        }
        env[2][kind] = store(kind, files, rejected=snapshot.rejected)

    def mutate(container, command, text, **kwargs):
        assert command[:4] == ["bash", "-c", io._MUTATE_SCRIPT, "bash"]
        kind = "issue" if command[4].endswith("issue-outbox") else "pr"
        sync(kind)
        command = list(command)
        command[4] = str(directory)
        result = run_shell(command, input=text, text=True, capture_output=True, check=True).stdout
        save(kind)
        return result

    env[1].exec_with_input.side_effect = mutate

    def execute(container, command, **kwargs):
        # Run the production container scripts; only the fixed paths are remapped.
        assert command[:2] == ["bash", "-c"], command
        command = list(command)
        if command[4] == io.store_directory("issue"):
            # Issue receipts pass the directory and a base64 payload, not a log path.
            kind = "issue"
            command[4] = str(directory)
        elif command[-1].endswith(("progress.json", "applied.log")):
            kind = "pr"
            command[-1] = str(directory / command[-1].rsplit("/", 1)[-1])
        else:
            raise AssertionError(command)
        sync(kind)
        result = run_shell(command, text=True, capture_output=True, check=True).stdout
        save(kind)
        return result

    env[1].exec.side_effect = execute
    mocker.patch.object(io, "read_store", side_effect=lambda i, c, k, **kw: env[2][k])
    mocker.patch.object(
        issue_outbox, "read_text_outbox", side_effect=lambda *a, **kw: env[2]["issue"].as_dict()
    )
    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected subprocess"))
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/repo.git")
    mocker.patch("jailbee.submodules.declared_submodule_remotes", return_value=())
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=[])
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    mocker.patch.object(issue_github, "list_labels", return_value={})
    mocker.patch.object(
        issue_github,
        "get_issue",
        return_value=issue_github.IssueSnapshot(42, "Old", "Body", (), "open", "url", False),
    )
    create = mocker.patch.object(
        issue_github,
        "create_issue",
        return_value=issue_github.MutationReceipt(
            issue=73, url="https://github.com/acme/repo/issues/73"
        ),
    )
    comment = mocker.patch.object(
        issue_github,
        "add_comment",
        return_value=issue_github.MutationReceipt(
            issue=42, url="https://github.com/acme/repo/issues/42#issuecomment-1"
        ),
    )
    review = mocker.patch.object(
        pr, "submit_review", return_value="https://github.com/acme/repo/pull/42#pullrequestreview-1"
    )
    mocker.patch.object(pr, "gh_login", return_value="alice")
    mocker.patch.object(
        pr,
        "resolve_pr",
        return_value=pr.PrInfo(
            number=42, head_ref="feature", head_sha="a" * 40, state="OPEN", base_ref="main"
        ),
    )
    env[1].config_get.side_effect = lambda c, key: "42" if key == "user.jailbee.pr" else None
    env[1].exec.return_value = ""
    payload = json.loads(env[2]["pr"].as_dict()["001.json"])
    payload["repo"] = "acme/repo"
    env[2]["pr"] = store("pr", {"001.json": json.dumps(payload), "002.json": json.dumps(payload)})
    files = env[2]["issue"].as_dict()
    env[2]["issue"] = store("issue", files | {"002.json": files["001.json"]})
    manager = PrManagement(tmp_path / "pr-locks")
    mocker.patch("jailbee.outbox.publish.PrManagement", return_value=manager)
    return env, create, comment, review


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("mode", ["yes", "cancel", "dry-run"])
def test_apply_real_domain_orchestration(publication_env, mocker, kind, mode):
    env, create, comment, review = publication_env
    prompt = mocker.spy(typer, "confirm")
    args = ["outbox", "apply", "feature", f"{kind}/001.json"]
    if mode == "yes":
        args += ["-y"]
    elif mode == "dry-run":
        args += ["--dry-run"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    if mode == "cancel":
        prompt.assert_called_once_with(
            f"Publish {3 if kind == 'issue' else 1} pending actions?", default=False
        )
    else:
        prompt.assert_not_called()
    if mode != "yes":
        create.assert_not_called()
        comment.assert_not_called()
        review.assert_not_called()
        env[1].exec.assert_not_called()
        assert not list(env[5].root.rglob("*.json"))
    elif kind == "issue":
        create.assert_called_once()
        assert [call.args[2] for call in comment.call_args_list] == [73, 42]
        assert "fully applied" in result.output
        receipts = [
            json.loads(line) for line in env[2]["issue"].as_dict()["applied.log"].splitlines()
        ]
        assert [(r["manifest"], r["index"], r["issue"]) for r in receipts] == [
            ("001.json", 0, 73),
            ("001.json", 1, 42),
            ("001.json", 2, 42),
        ]
        assert all(r["repo"] == "acme/repo" and r["url"] for r in receipts)
    else:
        review.assert_called_once()
    if mode == "yes":
        assert "001.json" not in env[2][kind].as_dict()
        assert "002.json" in env[2][kind].as_dict()


def _unowned(env):
    env[1].config_get.side_effect = lambda c, key: "43" if key == "user.jailbee.pr" else None


def test_apply_refuses_an_unowned_pr_with_yes_and_names_foreign(publication_env, mocker):
    env, _create, _comment, review = publication_env
    _unowned(env)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)

    result = CliRunner().invoke(app, ["outbox", "apply", "feature", "pr/001.json", "-y"])

    assert result.exit_code == 2, result.output
    assert "--foreign" in result.output
    review.assert_not_called()


def test_apply_publishes_to_an_unowned_pr_with_foreign(publication_env, mocker):
    env, _create, _comment, review = publication_env
    _unowned(env)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    result = CliRunner().invoke(
        app, ["outbox", "apply", "feature", "pr/001.json", "-y", "--foreign"]
    )

    assert result.exit_code == 0, result.output
    assert "not bound to container" in result.output
    review.assert_called_once()


def test_apply_at_a_terminal_warns_and_asks_about_an_unowned_pr(publication_env, mocker):
    env, _create, _comment, review = publication_env
    _unowned(env)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)

    result = CliRunner().invoke(app, ["outbox", "apply", "feature", "pr/001.json"], input="y\n")

    assert result.exit_code == 0, result.output
    assert result.output.index("not bound to container") < result.output.index("Publish 1")
    review.assert_called_once()


def test_apply_rejects_foreign_for_an_issue(publication_env):
    result = CliRunner().invoke(
        app, ["outbox", "apply", "feature", "issue/001.json", "-y", "--foreign"]
    )

    assert result.exit_code == 2, result.output
    assert "foreign" in result.output


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_domain_validation_exit_two(publication_env, mocker, kind):
    env, create, comment, review = publication_env
    mocker.patch("jailbee.git.get_remote_url", return_value="https://example.org/not-github.git")
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", "-y"])
    assert result.exit_code == 2, result.output
    create.assert_not_called()
    comment.assert_not_called()
    review.assert_not_called()
    env[1].exec.assert_not_called()


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_apply_first_store_read_timeout_is_execution(publication_env, mocker, kind):
    from jailbee.incus import IncusTimeoutError
    from jailbee.outbox import service
    from jailbee.outbox.models import OutboxExecutionError

    original = IncusTimeoutError("first reader timeout")
    failure = OutboxExecutionError("reader transport failed")
    failure.__cause__ = original
    mocker.patch.object(service, "read_store", side_effect=failure)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", "-y"])
    assert result.exit_code == 1, result.output
    assert "reader transport failed" in result.output
    assert_publication_refused(publication_env)


def test_apply_pr_execution_read_error_is_one(publication_env, mocker):
    from jailbee import pr

    mocker.patch.object(pr, "resolve_pr", side_effect=pr.PrError("GitHub read failed"))
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", "pr/001.json", "-y"])
    assert result.exit_code == 1
    assert "GitHub read failed" in result.output
    publication_env[3].assert_not_called()


def assert_publication_refused(publication_env):
    for mutation in publication_env[1:]:
        mutation.assert_not_called()
    env = publication_env[0]
    env[1].exec.assert_not_called()
    env[4].assert_not_called()
    assert not list(env[5].root.rglob("*.json"))


def issue_edit(env):
    files = env[2]["issue"].as_dict()
    files["001.json"] = json.dumps(
        {
            "version": 1,
            "actions": [
                {
                    "type": "edit",
                    "repo": ".",
                    "issue": 42,
                    "body": "New",
                    "expected": {"body": "Body"},
                }
            ],
        }
    )
    env[2]["issue"] = store("issue", files)


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("late", [False, True])
def test_apply_target_staleness_is_validation(publication_env, mocker, kind, late):
    from dataclasses import replace

    from jailbee import issue_github

    env = publication_env[0]
    if kind == "issue":
        issue_edit(env)

    def change():
        if kind == "issue":
            issue_github.get_issue.return_value = replace(
                issue_github.get_issue.return_value, body="Moved"
            )
        else:
            env[1].config_get.side_effect = lambda c, key: (
                "43" if key == "user.jailbee.pr" else None
            )

    if not late:
        change()

    def confirm(*args, **kwargs):
        change()
        return True

    prompt = mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 2, result.output
    assert result.stderr
    assert prompt.call_count == int(late)
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_content_changed_during_confirmation_is_validation(publication_env, mocker, kind):
    env = publication_env[0]

    def confirm(*args, **kwargs):
        files = env[2][kind].as_dict()
        payload = json.loads(files["001.json"])
        payload["actions"][0]["body"] = "Changed after approval"
        files["001.json"] = json.dumps(payload)
        env[2][kind] = store(kind, files)
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 2, result.output
    assert "refresh" in result.output
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("change", ["body", "missing", "identity"])
def test_apply_final_domain_validation_is_two(publication_env, mocker, change):
    from dataclasses import replace

    from jailbee import issue_outbox

    env = publication_env[0]
    armed = False
    reached = []
    read = issue_outbox.read_issue_outbox
    identify = issue_outbox.container_identity

    def final_read(*args, **kwargs):
        if armed and change != "identity":
            reached.append(change)
            files = env[2]["issue"].as_dict()
            if change == "missing":
                files.pop("001.json")
            else:
                files["body.md"] = "Changed at final authoritative read"
            env[2]["issue"] = store("issue", files)
        return read(*args, **kwargs)

    def final_identity(*args, **kwargs):
        value = identify(*args, **kwargs)
        if armed and change == "identity":
            reached.append(change)
            return replace(value, created_at="replacement")
        return value

    def confirm(*args, **kwargs):
        nonlocal armed
        armed = True
        return True

    mocker.patch.object(issue_outbox, "read_issue_outbox", side_effect=final_read)
    mocker.patch.object(issue_outbox, "container_identity", side_effect=final_identity)
    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", "issue/001.json"])
    assert result.exit_code == 2, result.output
    assert reached == [change]
    assert result.stderr
    assert_publication_refused(publication_env)


@pytest.mark.parametrize(
    "boundary, late",
    [
        ("login", False),
        ("issue", False),
        ("labels", False),
        ("outbox", False),
        ("pr-read", False),
        ("pr-login", False),
        ("issue", True),
        ("outbox", True),
        ("pr-read", True),
    ],
)
def test_apply_transport_failure_has_safe_execution_diagnostic(
    publication_env, mocker, boundary, late
):
    from jailbee import issue_github, issue_outbox, pr
    from jailbee.outbox_io import OutboxReadError

    env = publication_env[0]
    kind = "pr" if boundary.startswith("pr-") else "issue"
    if boundary == "issue":
        issue_edit(env)
    failure = "transport [bold]down[/bold]\x1b\r"
    owner, name, error = {
        "login": (issue_github, "current_login", issue_github.IssueGithubReadError),
        "issue": (issue_github, "get_issue", issue_github.IssueGithubReadError),
        "labels": (issue_github, "list_labels", issue_github.IssueGithubReadError),
        "outbox": (issue_outbox, "read_text_outbox", OutboxReadError),
        "pr-read": (pr, "resolve_pr", pr.PrError),
        "pr-login": (pr, "gh_login", pr.PrError),
    }[boundary]

    def fail():
        mocker.patch.object(owner, name, side_effect=error(failure))

    if not late:
        fail()

    def confirm(*args, **kwargs):
        fail()
        return True

    prompt = mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 1, result.output
    assert "transport [bold]down[/bold]" in result.stderr
    assert "\x1b" not in result.stderr and "\r" not in result.stderr
    assert prompt.call_count == int(late)
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_mutation_failure_is_execution_and_retains_progress(publication_env, kind):
    from jailbee import issue_github, pr
    from jailbee.outbox_io import journal_key

    env, create, comment, review = publication_env
    if kind == "issue":
        create.side_effect = issue_github.IssueGithubMutationError("secret detail", uncertain=True)
    else:
        review.side_effect = pr.PrError("write [bold]failed[/bold]\x1b")
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", "-y"])
    assert result.exit_code == 1, result.output
    assert "\x1b" not in result.stderr
    env[1].exec.assert_not_called()
    if kind == "issue":
        assert "secret detail" not in result.output
        assert "outcome is uncertain" in result.stderr
        assert env[5].load(journal_key(IDENTITY, "001.json")).actions[0].state == "uncertain"
        comment.assert_not_called()
        review.assert_not_called()
    else:
        assert "write [bold]failed[/bold]" in result.stderr
        assert "re-running skips what landed" in result.output
        create.assert_not_called()
        comment.assert_not_called()


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_confirmation_rechecks_scope(publication_env, mocker, kind):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    scope = mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session", return_value=RemoteRepoScope(frozenset())
    )

    def confirm(*args, **kwargs):
        scope.return_value = RemoteRepoScope(frozenset({"acme"}))
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 2, result.output
    for mutation in publication_env[1:]:
        mutation.assert_not_called()
    publication_env[0][1].exec.assert_not_called()


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_scope_refusal_precedes_local_reads(publication_env, mocker, kind):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset({"acme"})),
    )
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", "-y"])
    assert result.exit_code == 2, result.output
    publication_env[0][3].assert_not_called()
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_confirmation_reloads_target_config(publication_env, mocker, kind):
    env = publication_env[0]
    changed = env[0].model_copy(
        update={"container_user": env[0].container_user.model_copy(update={"uid": 2345})}
    )
    loader = mocker.patch("jailbee.config.load_repo_config", return_value=env[0])

    def confirm(*args, **kwargs):
        loader.return_value = changed
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 2, result.output
    assert "target config changed" in result.stderr
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_inspected_revision_refuses_drift_before_remote_gate(publication_env, mocker, kind):
    from jailbee import issue_github, pr

    shown = CliRunner().invoke(app, ["outbox", "show", "feature", f"{kind}/001.json", "-o", "json"])
    assert shown.exit_code == 0, shown.output
    token = json.loads(shown.stdout)["proposal"]["revision"]
    env = publication_env[0]
    files = env[2][kind].as_dict()
    payload = json.loads(files["001.json"])
    payload["actions"][0]["body"] = "Changed since inspection"
    files["001.json"] = json.dumps(payload)
    env[2][kind] = store(kind, files)
    prompt = mocker.patch("typer.confirm", side_effect=AssertionError("stale token prompted"))
    result = CliRunner().invoke(
        app, ["outbox", "apply", "feature", f"{kind}/001.json", "--revision", token, "-y"]
    )
    assert result.exit_code == 2, result.output
    assert "refresh" in result.stderr
    issue_github.current_login.assert_not_called()
    pr.resolve_pr.assert_not_called()
    prompt.assert_not_called()
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_read_only_plan_runs_one_domain_preflight(publication_env, kind):
    from jailbee import issue_github, pr

    result = CliRunner().invoke(
        app, ["outbox", "apply", "feature", f"{kind}/001.json", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    if kind == "issue":
        issue_github.current_login.assert_called_once()
        issue_github.get_issue.assert_called_once()
        issue_github.list_labels.assert_called_once()
    else:
        pr.resolve_pr.assert_called_once()
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_uses_target_owned_config_before_local_reads(
    publication_env, mocker, make_cfg, tmp_path, kind
):
    from jailbee import git
    from jailbee.outbox import io
    from tests.test_outbox_commands import register

    env = publication_env[0]
    caller_root = tmp_path / "caller"
    caller_root.mkdir()
    caller = make_cfg(caller_root, container_prefix="caller")
    target = env[0].model_copy(
        update={"container_user": env[0].container_user.model_copy(update={"uid": 2345})}
    )
    register("acme", target.repo_root)
    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=lambda root: caller if root == caller_root else target,
    )
    mocker.patch("jailbee.config.load_config", return_value=caller)
    reads = []

    def read(i, c, k, **kwargs):
        assert kwargs["uid"] == 2345
        assert c == "acme-feature"
        reads.append(k)
        return env[2][k]

    mocker.patch.object(service, "read_store", side_effect=read)
    mocker.patch.object(io, "read_store", side_effect=read)
    result = CliRunner().invoke(
        app,
        [
            "outbox",
            "apply",
            "acme-feature",
            f"{kind}/001.json",
            "--config",
            "caller.yaml",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert set(reads) == {"issue", "pr"}
    assert {call.args[0] for call in git.get_remote_url.call_args_list} == {target.repo_root}
    assert_publication_refused(publication_env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_retry_preserves_recorded_receipt_and_only_confirms_pending(
    publication_env, mocker, kind
):
    from jailbee import issue_github, pr
    from jailbee.outbox_io import journal_key

    env, create, comment, review = publication_env
    if kind == "issue":
        comment.side_effect = issue_github.IssueGithubMutationError("timeout", uncertain=True)
        first = CliRunner().invoke(app, ["outbox", "apply", "feature", "issue/001.json", "-y"])
        assert first.exit_code == 1, first.output
        journal = env[5].load(journal_key(IDENTITY, "001.json"))
        assert [(a.index, a.state, a.issue) for a in journal.actions] == [
            (0, "applied", 73),
            (1, "uncertain", None),
        ]
        create.reset_mock()
        comment.reset_mock()
        retry = CliRunner().invoke(app, ["outbox", "apply", "feature", "issue/001.json", "-y"])
        assert retry.exit_code == 2, retry.output
        assert "uncertain" in retry.stderr
        create.assert_not_called()
        comment.assert_not_called()
        env[1].exec.assert_not_called()
        assert env[5].load(journal_key(IDENTITY, "001.json")) == journal
    else:
        files = env[2][kind].as_dict()
        payload = json.loads(files["001.json"])
        payload["actions"].append({"type": "comment", "body": "Still pending"})
        files["001.json"] = json.dumps(payload)
        files["001.json.progress.json"] = json.dumps(
            {"applied": [0], "urls": {"0": "https://github.com/acme/repo/pull/42#review-1"}}
        )
        env[2][kind] = store(kind, files)
        pending = mocker.patch.object(pr, "add_issue_comment", return_value="receipt")
        prompt = mocker.spy(typer, "confirm")
        retry = CliRunner().invoke(app, ["outbox", "apply", "feature", "pr/001.json"], input="y\n")
        assert retry.exit_code == 0, retry.output
        prompt.assert_called_once_with("Publish 1 pending actions?", default=False)
        review.assert_not_called()
        pending.assert_called_once_with(env[0].repo_root, 42, "Still pending", repo="acme/repo")
        assert "Already published (skipped): [0]" in retry.output


@pytest.mark.parametrize("name", ["ls", "show", "drop", "apply", "browse"])
def test_colliding_container_uses_public_browse_leaf(env, name):
    env[6]["name"] = f"acme-{name}"
    env[1].exists.side_effect = lambda value: value == env[6]["name"]
    result = CliRunner().invoke(app, ["outbox", "browse", name])
    assert result.exit_code == 0, result.output
    assert f"acme-{name}" in result.output


# --- omitted container / proposal -------------------------------------------

_ISSUE = ProposalId("issue", "001.json")


def _only_issue(env):
    """Leave the issue proposal as the single pending one in the single container."""
    env[2]["pr"] = store("pr", {})


def _off_tty(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)


def _on_tty(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)


def test_show_without_arguments_takes_the_only_proposal(env, mocker):
    _off_tty(mocker)
    _only_issue(env)
    result = CliRunner().invoke(app, ["outbox", "show", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["proposal"]["actions"][0]["index"] == 0
    assert "Using container acme-feature" in result.stderr
    assert "Using proposal issue/001.json" in result.stderr


def test_show_with_several_proposals_off_a_tty_names_them(env, mocker):
    _off_tty(mocker)
    result = CliRunner().invoke(app, ["outbox", "show", "feature"])
    assert result.exit_code == 2
    text = panel_text(result.output)
    assert "issue/001.json" in text and "pr/001.json" in text


def test_drop_without_arguments_asks_even_for_one(env, mocker):
    _on_tty(mocker)
    _only_issue(env)
    select = mocker.patch("jailbee.prompting._select", side_effect=[IDENTITY.full_name, _ISSUE])
    result = CliRunner().invoke(app, ["outbox", "drop", "-y"])
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in select.call_args_list] == ["container", "proposal"]
    env[4].assert_called_once()


def test_drop_with_a_named_container_asks_only_for_the_proposal(env, mocker):
    _on_tty(mocker)
    select = mocker.patch("jailbee.prompting._select", return_value=_ISSUE)
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "-y"])
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in select.call_args_list] == ["proposal"]
    env[4].assert_called_once()


def test_drop_with_both_given_never_asks(env, mocker):
    _on_tty(mocker)
    select = mocker.patch("jailbee.prompting._select", side_effect=AssertionError("asked"))
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "issue/001.json", "-y"])
    assert result.exit_code == 0, result.output
    select.assert_not_called()


@pytest.mark.parametrize("answers", [[None], [IDENTITY.full_name, None]])
def test_drop_cancel_at_either_prompt_drops_nothing(env, mocker, answers):
    _on_tty(mocker)
    _only_issue(env)
    mocker.patch("jailbee.prompting._select", side_effect=answers)
    result = CliRunner().invoke(app, ["outbox", "drop", "-y"])
    assert result.exit_code == 1, result.output
    env[4].assert_not_called()


@pytest.mark.parametrize("answers", [[None], [IDENTITY.full_name, None]])
def test_apply_cancel_at_either_prompt_applies_nothing(env, mocker, answers):
    _on_tty(mocker)
    _only_issue(env)
    mocker.patch("jailbee.prompting._select", side_effect=answers)
    applied = mocker.patch("jailbee.outbox.commands.apply_selected", return_value=0)
    result = CliRunner().invoke(app, ["outbox", "apply", "-y"])
    assert result.exit_code == 1, result.output
    applied.assert_not_called()


def test_apply_asks_even_for_one_and_passes_the_choice(env, mocker):
    _on_tty(mocker)
    _only_issue(env)
    select = mocker.patch("jailbee.prompting._select", side_effect=[IDENTITY.full_name, _ISSUE])
    applied = mocker.patch("jailbee.outbox.commands.apply_selected", return_value=0)
    result = CliRunner().invoke(app, ["outbox", "apply", "-y"])
    assert result.exit_code == 0, result.output
    assert select.call_count == 2
    assert applied.call_args.args[2:4] == (IDENTITY.full_name, _ISSUE)


def test_apply_without_arguments_off_a_tty_names_candidates(env, mocker):
    _off_tty(mocker)
    _only_issue(env)
    applied = mocker.patch("jailbee.outbox.commands.apply_selected", return_value=0)
    result = CliRunner().invoke(app, ["outbox", "apply"])
    assert result.exit_code == 2
    assert "acme-feature" in panel_text(result.output)
    applied.assert_not_called()


@pytest.mark.parametrize("leaf", ["show", "drop", "apply"])
def test_no_pending_proposals_is_a_reason_with_no_side_effect(env, mocker, leaf):
    _on_tty(mocker)
    env[2]["pr"] = store("pr", {})
    env[2]["issue"] = store("issue", {})
    select = mocker.patch("jailbee.prompting._select", side_effect=AssertionError("asked"))
    result = CliRunner().invoke(app, ["outbox", leaf])
    assert result.exit_code == 2, result.output
    assert "no container has pending proposals" in panel_text(result.output)
    select.assert_not_called()
    env[4].assert_not_called()


def test_drop_named_container_without_proposals_is_a_reason(env, mocker):
    _on_tty(mocker)
    env[2]["pr"] = store("pr", {})
    env[2]["issue"] = store("issue", {})
    select = mocker.patch("jailbee.prompting._select", side_effect=AssertionError("asked"))
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "-y"])
    assert result.exit_code == 2, result.output
    assert "no pending proposals in feature" in panel_text(result.output)
    select.assert_not_called()
    env[4].assert_not_called()


def test_drop_named_unavailable_container_reports_the_error_not_none(env, mocker):
    _on_tty(mocker)
    broken = ContainerView(
        None, "acme-feature", False, "container acme-feature is not running", (), ()
    )
    mocker.patch("jailbee.outbox.commands.discover", return_value=(broken,))
    select = mocker.patch("jailbee.prompting._select", side_effect=AssertionError("asked"))
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "-y"])
    assert result.exit_code == 2, result.output
    text = panel_text(result.output)
    assert "acme-feature is not running" in text
    assert "no pending proposals" not in text
    select.assert_not_called()
    env[4].assert_not_called()


def test_drop_named_container_with_one_proposal_still_asks(env, mocker):
    """`destructive=True` must hold on the named path: a lone proposal is not auto-taken."""
    _on_tty(mocker)
    _only_issue(env)
    select = mocker.patch("jailbee.prompting._select", return_value=_ISSUE)
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "-y"])
    assert result.exit_code == 0, result.output
    assert select.call_count == 1
    env[4].assert_called_once()


@pytest.mark.parametrize(("kind", "foreign"), [("pr", True), ("issue", False)])
def test_browse_publish_consents_to_a_foreign_pr_only_for_pr_proposals(env, mocker, kind, foreign):
    """The browser always asks under the plan, so its consent covers a PR the
    container does not own; an issue proposal must not carry the PR-only flag."""
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.cli_outbox.browser_read_only", return_value=False)
    actions = {}
    mocker.patch(
        "jailbee.outbox.browser.run_browser",
        side_effect=lambda a, _container: actions.setdefault("a", a) and 0,
    )
    applied = mocker.patch("jailbee.outbox.commands.apply_selected", return_value=0)

    result = CliRunner().invoke(app, ["outbox", "browse", "feature"])
    assert result.exit_code == 0, result.output
    actions["a"].publish("feature", ProposalId(kind, "001.json"), "a" * 64)

    assert applied.call_args.kwargs["options"].foreign is foreign
