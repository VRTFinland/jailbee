"""Representative commands ask a dashboard exactly what they ask at the terminal.

Each command runs twice: once on a terminal, where the terminal backend is
recorded and answers, and once with a prompt channel peer on a socketpair
(`JAILBEE_PROMPT_FD`). The questions seen on each side are normalized to the same
shape and compared, then the outcome is checked to follow the answer.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
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
    destructive: bool


# What the user answers, by question: a bool for a confirmation, an option title for a choice.
Answer = Callable[[Asked], Any]


def _title_of(options: Any, value: Any) -> str | None:
    return next((o.title for o in options if value is not None and o.value == value), None)


@pytest.fixture
def at_terminal(monkeypatch: pytest.MonkeyPatch, mocker: Any) -> Callable[[Answer], list[Asked]]:
    """Make the process a terminal; record what the terminal backend is asked."""

    def start(answer: Answer) -> list[Asked]:
        asked: list[Asked] = []
        mocker.patch("jailbee.prompting.is_interactive", return_value=True)

        def confirm(
            self: Any, question: str, *, default: bool, destructive: bool, **_: Any
        ) -> bool:
            item = Asked("confirm", question, (), False if destructive else default, destructive)
            asked.append(item)
            return bool(answer(item))

        def choose_one(
            self: Any,
            noun: str,
            options: Any,
            *,
            destructive: bool,
            question: str | None,
            default: Any = None,
            **_: Any,
        ) -> Any:
            item = Asked(
                "choose_one",
                noun if question is None else question,
                tuple(o.title for o in options),
                _title_of(options, default),
                destructive,
            )
            asked.append(item)
            title = answer(item)
            return next(o.value for o in options if o.title == title)

        monkeypatch.setattr(prompting.TerminalBackend, "confirm", confirm)
        monkeypatch.setattr(prompting.TerminalBackend, "choose_one", choose_one)
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
            item = Asked(
                p.kind,
                p.question,
                tuple(o.title for o in p.options),
                titles.get(str(p.default)) if p.kind == "choose_one" else p.default,
                p.destructive,
            )
            asked.append(item)
            reply = answer(item)
            if p.kind == "choose_one":
                return next(o.id for o in p.options if o.title == reply)
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
    assert terminal[0].destructive is True
    assert terminal[0].default is False
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
