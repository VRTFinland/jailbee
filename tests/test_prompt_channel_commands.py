"""Representative commands ask a dashboard exactly what they ask at the terminal.

Each command runs twice: once on a terminal, where the terminal backend is
recorded and answers, and once with a prompt channel peer on a socketpair
(`JAILBEE_PROMPT_FD`). The questions seen on each side are normalized to the same
shape and compared, then the outcome is checked to follow the answer.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jailbee import prompt_channel as pc
from jailbee import prompting
from jailbee.cli import app
from tests.test_branch_pr import source_publisher as source_publisher
from tests.test_cli_outbox import env as env
from tests.test_cli_outbox import publication_env as publication_env
from tests.test_prompt_channel import Peer


@dataclass(frozen=True)
class Asked:
    kind: str
    question: str
    options: tuple[str, ...]
    default: Any
    # The terminal draws no "destructive" flag, so only the dashboard side knows it.
    destructive: bool = field(default=False, compare=False)
    detail: str | None = None


# What the user answers, by question: a bool for a confirmation, an option title for a choice,
# a list of titles for a multiple choice.
Answer = Callable[[Asked], Any]


@pytest.fixture
def at_terminal(monkeypatch: pytest.MonkeyPatch, mocker: Any) -> Callable[[Answer], list[Asked]]:
    """Make the process a terminal; record what the terminal itself renders.

    Nothing of `prompting`'s own question logic is stubbed: the recording sits at
    the renderers' libraries (`typer.confirm`, `questionary.select`, the `tui`
    checkbox), so the question text, rows and defaults are what the terminal
    would actually draw, independent of the channel backend's code.
    """

    def start(answer: Answer) -> list[Asked]:
        asked: list[Asked] = []
        mocker.patch("jailbee.prompting.is_interactive", return_value=True)
        details: list[str] = []
        monkeypatch.setattr(prompting, "_show_detail", details.append)

        def confirm(text: str, default: bool = False, **_: Any) -> bool:
            item = Asked("confirm", text, (), default, detail=details.pop() if details else None)
            asked.append(item)
            return bool(answer(item))

        def select(message: str, choices: Any, **kwargs: Any) -> Any:
            rows = [c for c in choices if c.title != "cancel"]
            preselected = kwargs.get("default")
            item = Asked(
                "choose_one",
                message,
                tuple(c.title for c in rows),
                None if preselected is None else preselected.title,
            )
            asked.append(item)
            title = answer(item)
            value = next(c.value for c in rows if c.title == title)
            return mocker.Mock(ask=lambda: value)

        def checkbox(message: str, choices: Any, **_: Any) -> list[Any]:
            item = Asked(
                "choose_many",
                message,
                tuple(c.title for c in choices),
                tuple(c.title for c in choices if getattr(c, "checked", False)),
            )
            asked.append(item)
            ticked = answer(item)
            return [c.value for c in choices if c.title in ticked]

        mocker.patch("typer.confirm", side_effect=confirm)
        mocker.patch("questionary.select", side_effect=select)
        mocker.patch("jailbee.tui.checkbox", side_effect=checkbox)
        return asked

    return start


def _open_channel(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, handler: Callable[..., Any]
) -> Peer:
    """A socketpair whose command end is exported as `JAILBEE_PROMPT_FD` from now on."""
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))
    peer = Peer(ui_sock, handler)

    def close() -> None:
        # Command side first: the peer's serve loop ends on EOF.
        prompting.drop_channel()
        cmd_sock.close()
        peer.join()
        ui_sock.close()

    request.addfinalizer(close)
    return peer


@pytest.fixture
def at_dashboard(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Callable[[Answer], list[Asked]]:
    """Attach a prompt channel peer when called; record what it is asked."""

    def start(answer: Answer) -> list[Asked]:
        asked: list[Asked] = []

        def handler(p: pc.Prompt) -> Any:
            titles = {o.id: o.title for o in p.options}
            if p.kind == "choose_one":
                default: Any = titles.get(str(p.default))
            elif p.kind == "choose_many":
                default = tuple(titles[i] for i in (p.default or ()))
            else:
                default = p.default
            item = Asked(
                p.kind,
                p.question,
                tuple(o.title for o in p.options),
                default,
                p.destructive,
                p.detail,
            )
            asked.append(item)
            reply = answer(item)
            if p.kind == "choose_one":
                return next(o.id for o in p.options if o.title == reply)
            if p.kind == "choose_many":
                return [o.id for o in p.options if o.title in reply]
            return reply

        _open_channel(monkeypatch, request, handler)
        return asked

    return start


def _both(
    at_terminal: Callable[[Answer], list[Asked]],
    at_dashboard: Callable[[Answer], list[Asked]],
    answer: Answer,
    run: Callable[[], Any],
) -> tuple[list[Asked], list[Asked], Any, Any]:
    """Run the command at the terminal, then at a dashboard; return both views."""
    terminal_asked = at_terminal(answer)
    terminal_result = run()
    dashboard_asked = at_dashboard(answer)
    dashboard_result = run()
    return terminal_asked, dashboard_asked, terminal_result, dashboard_result


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_outbox_publish_questions_match_terminal(publication_env, at_terminal, at_dashboard, kind):
    terminal, dashboard, tr, dr = _both(
        at_terminal,
        at_dashboard,
        lambda q: False,
        lambda: CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", *(["--draft"] if kind == "pr" else [])]),
    )
    assert tr.exit_code == dr.exit_code == 0, (tr.output, dr.output)
    assert terminal == dashboard and terminal
    assert terminal[-1].kind in ("confirm", "choose_one")
    _, create, comment, review = publication_env
    create.assert_not_called()
    comment.assert_not_called()
    review.assert_not_called()


def test_own_source_picker_has_identical_terminal_and_dashboard_rows(at_terminal, at_dashboard):
    from jailbee.branch_pr import PendingSource, choose_pr_source

    pending = [
        PendingSource("own.json", "Own", "refs/heads/topic", "main", "topic", 3),
        PendingSource("child.json", "Child", "refs/jailbee/from/child/topic", "main"),
    ]
    terminal, dashboard, first, second = _both(
        at_terminal,
        at_dashboard,
        lambda asked: asked.options[0],
        lambda: choose_pr_source(pending, own_label="container branch"),
    )
    assert terminal == dashboard
    assert len(terminal) == 1 and len(terminal[0].options) == 2
    assert "3 newer commits" in terminal[0].options[0]
    assert first == second == "own.json"


# ---- PR images: approval before uploading ------------------------------------------


@pytest.fixture(params=["create", "submodule", "update"])
def image_pr_command(request, mocker, monkeypatch, tmp_path):
    from jailbee import pr_outbox
    from tests.test_branch_pr import StoreFiles
    from tests.test_branch_pr_attachments import prepare
    from tests.test_cli_pr import _pr_created, _publish_result, _setup, _update_setup
    from tests.test_cli_submodule_pr import _happy
    from tests.test_cli_submodule_pr import _setup as submodule_setup
    from tests.test_pr_description_attachments import setup_upload

    read_outbox = pr_outbox.read_outbox
    pending_pr_text = pr_outbox.pending_pr_text
    if request.param == "submodule":
        _cfg, incus, _ = submodule_setup(mocker, tmp_path)
        create = _happy(mocker)
        mocker.patch("jailbee.pr_outbox.read_outbox", side_effect=read_outbox)
        argv = ["submodule", "pr", "push", "feat-foo", "lib/a", "--yes", "--no-ai"]
        repo = "acme/lib-a"
    else:
        setup = _update_setup if request.param == "update" else _setup
        _cfg, incus = setup(mocker, tmp_path)
        mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
        mocker.patch("jailbee.pr_submodule_flow.publish_submodule_prs_first", return_value=[])
        create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
        argv = ["pr", "push", "feat-foo", "--no-ai"]
        repo = "acme/widgets"
    incus.config_get_checked.return_value = None
    # Both command families select a real staged image description; only transport
    # and GitHub operations are mocked by their existing command fixtures.
    files = StoreFiles("uuid-full")
    prepare({"files": files, "data": {"version": 1, "repo": repo, "pr": None, "head_sha": None}})
    # The legacy submodule fixture's instance has no uuid until it needs an outbox.
    incus.list_containers.return_value[0]["config"] = {"volatile.uuid": "uuid-full"}
    source = read_outbox(incus, "sampleapp-feat-foo", uid=1000, strict=True)
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=source)
    # Restore selection rather than the legacy fixtures' empty-outbox default.
    mocker.patch("jailbee.pr_outbox.pending_pr_text", wraps=pending_pr_text)
    uploads = setup_upload(monkeypatch)
    edit = mocker.patch("jailbee.pr.edit_pr")
    return argv, files, uploads, edit if request.param == "update" else create, request.param


@pytest.mark.parametrize("backend", ["terminal", "dashboard"])
@pytest.mark.parametrize("consent", [True, False])
def test_pr_images_ask_before_uploading(
    image_pr_command, at_terminal, at_dashboard, backend, consent
):
    argv, files, uploads, publish, operation = image_pr_command
    start = at_terminal if backend == "terminal" else at_dashboard
    asked = start(lambda _asked: consent)

    result = CliRunner().invoke(app, argv)

    verb = "Update" if operation == "update" else "Create"
    assert asked == [
        Asked("confirm", f"{verb} PR with this description and upload its attachments?", (), False)
    ]
    assert (result.exit_code == 0) is consent, result.output
    assert bool(uploads) is consent
    assert publish.called is consent
    assert ("m.json" not in files) is consent


@pytest.mark.parametrize("backend", ["terminal", "dashboard", "noninteractive"])
def test_pr_images_never_upload_without_an_answer(
    image_pr_command, mocker, monkeypatch, request, backend
):
    argv, files, uploads, publish, _operation = image_pr_command
    if backend == "dashboard":
        _open_channel(monkeypatch, request, lambda _: None)
    else:
        mocker.patch("jailbee.prompting.is_interactive", return_value=backend == "terminal")
        if backend == "noninteractive":
            monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")

    result = CliRunner().invoke(app, argv, input="\x03")

    assert result.exit_code == (2 if backend == "noninteractive" else 1), result.output
    expected_message = "confirmation needed" if backend == "noninteractive" else "Aborted"
    assert expected_message in result.output
    assert not uploads
    publish.assert_not_called()
    assert "m.json" in files


# ---- uncertain uploads: attestation or authorizing a duplicate ----------------------


@pytest.fixture
def uncertain_upload_command(source_publisher, monkeypatch, mocker):
    from jailbee import attachment_github
    from jailbee.outbox.attachment_publication import AttachmentPublicationError
    from jailbee.outbox.pr_description import create_with_description
    from tests.test_branch_pr_attachments import URL, prepare
    from tests.test_pr_description_attachments import selected, setup_upload

    p = source_publisher
    descriptor = prepare(p)
    source = selected(p)
    setup_upload(monkeypatch)
    mocker.patch(
        "jailbee.attachment_github.upload_to_target",
        side_effect=attachment_github.UploadUncertainError("connection lost"),
    )
    with pytest.raises(AttachmentPublicationError):
        create_with_description(
            p["cfg"],
            p["incus"],
            "full",
            source,
            source.text.body,
            p["create"],
            lambda _: True,
            publisher="user",
        )
    mocker.patch("jailbee.config.load_repo_config", return_value=p["cfg"])
    mocker.patch("jailbee.incus.Incus", return_value=p["incus"])
    mocker.patch(
        "jailbee.outbox.attachment_recovery.resolve_target", return_value=(p["cfg"], "full")
    )
    argv = ["outbox", "attachment-resolve", "full", "pr/m.json", descriptor["blob"], "1"]
    return argv, descriptor, URL


@pytest.mark.parametrize("backend", ["terminal", "dashboard"])
@pytest.mark.parametrize("retry_duplicate", [False, True])
@pytest.mark.parametrize("consent", [False, True])
def test_upload_recovery_asks_the_same_confirmation(
    uncertain_upload_command, at_terminal, at_dashboard, backend, consent, retry_duplicate
):
    from jailbee.outbox.publication_db import open_publications

    argv, _descriptor, url = uncertain_upload_command
    options = ["--retry-duplicate"] if retry_duplicate else ["--url", url]
    argv = argv[:2] + options + ["--"] + argv[2:]
    start = at_terminal if backend == "terminal" else at_dashboard
    asked = start(lambda _: consent)

    result = CliRunner().invoke(app, argv)

    assert asked == [Asked("confirm", "Resolve this upload?", (), False)], result.output
    assert result.exit_code == 0, result.output
    store = open_publications()
    with store.claim("uuid-full", "pr", "m.json") as claim:
        digest = store.stored_digest("uuid-full", "pr", "m.json")
        outcomes = store.image_publisher.outcomes(claim, digest)
    expected_state = ("rejected" if retry_duplicate else "applied") if consent else "uncertain"
    assert outcomes[0].state == expected_state
    assert outcomes[0].url == (url if consent and not retry_duplicate else None)


@pytest.mark.parametrize("backend", ["terminal", "dashboard", "noninteractive"])
@pytest.mark.parametrize("yes", [False, True])
def test_upload_recovery_requires_an_answer_unless_yes(
    uncertain_upload_command, mocker, monkeypatch, request, backend, yes
):
    from jailbee.outbox.publication_db import open_publications

    argv, _descriptor, url = uncertain_upload_command
    options = ["--url", url] + (["--yes"] if yes else [])
    # Blob identifiers use URL-safe base64 and can start with an option-like '-'.
    argv = argv[:2] + options + ["--"] + argv[2:]
    if backend == "dashboard":
        peer = _open_channel(monkeypatch, request, lambda _: None)
    else:
        mocker.patch("jailbee.prompting.is_interactive", return_value=backend == "terminal")
        if backend == "noninteractive":
            monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    terminal_prompt = mocker.spy(prompting, "_confirm")

    result = CliRunner().invoke(app, argv, input="\x03")

    expected_code = 0 if yes else (2 if backend == "noninteractive" else 1)
    assert result.exit_code == expected_code, result.output
    if yes:
        terminal_prompt.assert_not_called()
        if backend == "dashboard":
            assert not peer.prompts
    elif backend == "noninteractive":
        assert "--yes" in result.output
    else:
        assert "Aborted" in result.output
    store = open_publications()
    with store.claim("uuid-full", "pr", "m.json") as claim:
        digest = store.stored_digest("uuid-full", "pr", "m.json")
        outcomes = store.image_publisher.outcomes(claim, digest)
    assert outcomes[0].state == ("applied" if yes else "uncertain")


# ---- destroy: a destructive confirmation --------------------------------------------


def _destroy_env(tmp_path: Path, mocker: Any) -> Any:
    from tests.test_cli_destroy import _at_risk, _clone_payload, _setup

    incus, destroy = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    mocker.patch("jailbee.git_status.probe_container_git", return_value=_at_risk())
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    return destroy


@pytest.mark.parametrize("consent", [True, False])
def test_destroy_asks_the_same_destructive_confirmation(
    tmp_path, mocker, at_terminal, at_dashboard, consent
):
    destroy = _destroy_env(tmp_path, mocker)
    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda _asked: consent,
        lambda: CliRunner().invoke(app, ["destroy", "feat-a"]),
    )

    assert len(terminal) == 1
    assert terminal[0].kind == "confirm"
    assert terminal[0].default is False
    assert dashboard[0].destructive is True
    # What a destroy would lose is shown with the question, not only on stdout.
    assert terminal[0].detail
    assert dashboard == terminal
    assert destroy.call_count == (2 if consent else 0)  # once per run when consented
    assert (t_result.exit_code == 0) == (d_result.exit_code == 0) == consent


def test_destroy_cancelled_over_the_channel_destroys_nothing(
    tmp_path, mocker, monkeypatch, request
):
    destroy = _destroy_env(tmp_path, mocker)
    _open_channel(monkeypatch, request, lambda _p: None)  # the dashboard user cancels

    result = CliRunner().invoke(app, ["destroy", "feat-a"])

    assert result.exit_code != 0
    destroy.assert_not_called()


# ---- git push: choosing the action --------------------------------------------------


def _push_env(mocker: Any) -> Any:
    from jailbee.sync import MergeInContainerResult, PushResult
    from tests.test_cli import _push_cfg_factory

    cfg = _push_cfg_factory(action="ask", source="default-branch")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "full-name"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-x")
    mocker.patch("jailbee.git.detect_default_branch", return_value="main")
    pushed = PushResult(
        source="main",
        source_ref="refs/heads/main",
        container_ref="refs/jailbee/host/main",
        old_oid=None,
        new_oid="oid",
    )
    mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=pushed, container_branch="feat-x", fast_forward_only=True, head_oid="head"
        ),
    )
    return mocker.patch("jailbee.sync.push_to_container", return_value=pushed)


def test_push_asks_the_same_action_choice(mocker, at_terminal, at_dashboard):
    plain = _push_env(mocker)
    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda asked: "plain (transport only, no merge/rebase)",
        lambda: CliRunner().invoke(app, ["git", "push", "feat-x"]),
    )

    assert len(terminal) == 1
    assert terminal[0].kind == "choose_one"
    assert terminal[0].question == "What to do with the pushed ref?"
    assert terminal[0].options == (
        "merge into container's branch",
        "rebase container's branch onto pushed ref",
        "plain (transport only, no merge/rebase)",
    )
    assert dashboard == terminal
    assert t_result.exit_code == d_result.exit_code == 0, (t_result.output, d_result.output)
    assert plain.call_count == 2  # the answer chose the plain transport both times


# ---- shared credential: which login the group keeps ---------------------------------


def _credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path]:
    from tests.test_accounts_claude import _two_credentials

    return _two_credentials(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "title_prefix, kept",
    [("the group's login", "group"), ("this repo's login", "repo")],
)
def test_shared_credential_asks_the_same_choice(
    tmp_path, monkeypatch, at_terminal, at_dashboard, title_prefix, kept
):
    from tests.test_accounts_claude import _holder_cred, _prepare

    def pick(asked: Asked) -> str:
        return next(t for t in asked.options if t.startswith(title_prefix))

    cfg, _holder, repo_cred = _credentials(tmp_path, monkeypatch)
    terminal_asked = at_terminal(pick)
    _prepare(cfg)
    assert _holder_cred(cfg).read_text() == kept
    # Reset the two competing logins and ask again through the channel.
    _holder_cred(cfg).write_text("group")
    repo_cred.write_text("repo")
    dashboard_asked = at_dashboard(pick)
    _prepare(cfg)

    assert len(terminal_asked) == 1
    assert terminal_asked[0].question == "Which login should the group keep?"
    assert dashboard_asked == terminal_asked
    assert _holder_cred(cfg).read_text() == kept
    assert not repo_cred.exists()


# ---- pickers that draw their own header or rows --------------------------------------


def test_account_picker_asks_the_same_choice(at_terminal, at_dashboard):
    from types import SimpleNamespace

    from jailbee import cli
    from jailbee.accounts.models import Slot

    choices = [
        (SimpleNamespace(name="claude"), Slot("me@corp.com#c0ffee12", Path("/s/a"), live=False)),
        (SimpleNamespace(name="codex"), Slot("you@corp.com", Path("/s/b"), live=False)),
    ]

    def run() -> Any:
        return cli._choose_account_choice(
            choices, ref=None, nothing="nothing", message="Switch this repo to:"
        )

    terminal, dashboard, t_result, d_result = _both(
        at_terminal, at_dashboard, lambda asked: asked.options[1], run
    )

    assert terminal[0].question == "Switch this repo to:"
    assert len(terminal[0].options) == 2
    assert dashboard == terminal
    assert t_result == d_result == choices[1]


def test_group_container_picker_asks_the_same_choice(mocker, at_terminal, at_dashboard):
    from types import SimpleNamespace

    from jailbee.cli import _resolve_group_container

    cfg = SimpleNamespace(container_prefix="myrepo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "myrepo-a"}, {"name": "myrepo-b"}]
    mocker.patch(
        "jailbee.accounts.groups.effective_group",
        side_effect=lambda _cfg, _incus, name: "team" if name == "myrepo-a" else None,
    )

    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda asked: asked.options[0],
        lambda: _resolve_group_container(cfg, incus, None),
    )

    assert terminal[0].question == "Change the group of:"
    assert "team" in terminal[0].options[0]  # the fact the user is about to change
    assert "no group" in terminal[0].options[1]
    assert dashboard == terminal
    assert t_result == d_result == "myrepo-a"


def _container(name: str, state: str = "Running") -> Any:
    from jailbee.lifecycle import ContainerInfo

    return ContainerInfo(
        name=name, state=state, network="strict", ip="10.0.0.5", memory_limit="4GB", repo="myrepo"
    )


def test_container_picker_asks_the_same_choice(mocker, at_terminal, at_dashboard):
    from types import SimpleNamespace

    from jailbee import lifecycle

    cfg = SimpleNamespace(container_prefix="myrepo")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_container("myrepo-a"), _container("myrepo-b", "Stopped")],
    )

    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda asked: asked.options[1],
        lambda: lifecycle.resolve_container_for_interactive(cfg, mocker.MagicMock(), None),
    )

    assert terminal[0].question == "Select a container:"
    assert "Stopped" in terminal[0].options[1]  # the table row, not just the name
    assert dashboard == terminal
    assert t_result == d_result == "myrepo-b"


def test_submodule_multi_picker_asks_the_same_question_with_everything_ticked(
    at_terminal, at_dashboard
):
    from jailbee import tui
    from jailbee.submodule_pr import SubCandidate

    subs = [
        SubCandidate(
            path=path,
            commits=2,
            branch="feat/x",
            dirty=False,
            head_sha="aaa",
            recorded_sha="bbb",
            subject="work",
        )
        for path in ("lib/a", "lib/b")
    ]

    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda asked: asked.options[:1],  # untick the second
        lambda: tui.pick_submodules_multi(subs),
    )

    assert terminal[0].question == "Publish these submodule PRs first?"
    assert terminal[0].default == terminal[0].options  # every row starts ticked
    assert dashboard == terminal
    assert t_result == d_result == ["lib/a"]


def test_containers_multi_picker_asks_the_same_question(at_terminal, at_dashboard):
    from jailbee import tui

    containers = [_container("myrepo-a"), _container("myrepo-b")]

    terminal, dashboard, t_result, d_result = _both(
        at_terminal,
        at_dashboard,
        lambda asked: asked.options[1:],
        lambda: tui.pick_containers_multi(containers, message="Select containers to merge FROM:"),
    )

    assert terminal[0].question == "Select containers to merge FROM:"
    assert dashboard == terminal
    assert t_result == d_result == ["myrepo-b"]


def test_agent_use_asks_the_same_profile_and_tier_questions(mocker, at_terminal, at_dashboard):
    from unittest.mock import MagicMock

    from jailbee import agent_default
    from jailbee.orchestration.runner_model import Runner

    cfg = MagicMock()
    cfg.orchestration_view.return_value.default_runner = Runner(
        agent="claude-jb", profile="grok", model="sonnet"
    )
    mocker.patch.object(
        agent_default,
        "catalog",
        return_value={"claude": ("opus",), "grok": ("opus", "sonnet"), "codex": ()},
    )

    def answer(asked: Asked) -> Any:
        return asked.options[0] if asked.question.startswith("Default agent") else asked.options[1]

    def run() -> Any:
        return agent_default.choose(cfg, MagicMock(), MagicMock(), None)

    terminal, dashboard, t_result, d_result = _both(at_terminal, at_dashboard, answer, run)

    assert [a.question for a in terminal] == [
        "Default agent profile for this repo's containers:",
        "Model tier for claude:",
    ]
    assert terminal[0].options == ("claude", "grok (current)", "codex")
    assert terminal[0].default == "grok (current)"
    assert dashboard == terminal
    assert t_result == d_result
