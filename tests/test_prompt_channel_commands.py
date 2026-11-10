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
