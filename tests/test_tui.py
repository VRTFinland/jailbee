"""Tests for tui helpers."""

import pytest

from jailbee.lifecycle import ContainerInfo
from jailbee.tui import checkbox, pick_container, pick_containers_multi


def _info(
    name: str,
    state: str = "Running",
    network: str | None = "strict",
    ip: str | None = "10.0.0.5",
    repo: str | None = "myrepo",
) -> ContainerInfo:
    return ContainerInfo(
        name=name, state=state, network=network, ip=ip, memory_limit="4GB", repo=repo
    )


def test_pick_container_builds_choice_per_container(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "myrepo-feat-b"

    containers = [_info("myrepo-feat-a"), _info("myrepo-feat-b", state="Stopped")]
    result = pick_container(containers)

    assert result == "myrepo-feat-b"
    assert select.call_count == 1
    args, kwargs = select.call_args
    assert args[0] == "Select a container:"
    choices = kwargs["choices"]
    # Values stay as the full Incus name (used by downstream commands)
    assert [c.value for c in choices] == ["myrepo-feat-a", "myrepo-feat-b"]
    # Titles show the short name with the repo prefix stripped
    assert "feat-a" in choices[0].title
    assert "myrepo-feat-a" not in choices[0].title
    assert "Running" in choices[0].title
    assert "strict" in choices[0].title
    assert "10.0.0.5" in choices[0].title
    assert "Stopped" in choices[1].title


def test_pick_container_returns_none_when_cancelled(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = None

    assert pick_container([_info("only")]) is None


def test_pick_container_handles_missing_network_and_ip(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "only"

    pick_container([_info("only", state="Stopped", network=None, ip=None)])

    choices = select.call_args.kwargs["choices"]
    # Dashes should stand in for None fields
    assert " - " in choices[0].title  # network or ip rendered as "-"


def test_pick_containers_multi_returns_selected_full_names(mocker):
    cb = mocker.patch("jailbee.tui.checkbox")
    cb.return_value = ["myrepo-feat-a", "myrepo-feat-c"]

    containers = [
        _info("myrepo-feat-a"),
        _info("myrepo-feat-b", state="Stopped"),
        _info("myrepo-feat-c", network="loose", ip=None),
    ]
    result = pick_containers_multi(containers)

    assert result == ["myrepo-feat-a", "myrepo-feat-c"]
    assert cb.call_count == 1
    args, kwargs = cb.call_args
    assert args[0] == "Select containers to destroy:"
    choices = kwargs["choices"]
    assert [c.value for c in choices] == [
        "myrepo-feat-a",
        "myrepo-feat-b",
        "myrepo-feat-c",
    ]
    # Titles show the short name (prefix stripped) plus state/network/ip
    assert "feat-a" in choices[0].title
    assert "myrepo-feat-a" not in choices[0].title
    assert "Running" in choices[0].title
    assert "Stopped" in choices[1].title
    assert "loose" in choices[2].title
    # Missing ip rendered as "-"
    assert " - " in choices[2].title or choices[2].title.endswith("-")


def test_pick_containers_multi_returns_none_when_cancelled(mocker):
    cb = mocker.patch("jailbee.tui.checkbox")
    cb.return_value = None

    assert pick_containers_multi([_info("only")]) is None


def test_pick_containers_multi_returns_empty_list_when_no_box_ticked(mocker):
    cb = mocker.patch("jailbee.tui.checkbox")
    cb.return_value = []

    assert pick_containers_multi([_info("only")]) == []


def test_pick_containers_multi_uses_custom_message(mocker):
    cb = mocker.patch("jailbee.tui.checkbox")
    cb.return_value = []

    pick_containers_multi([_info("only")], message="Pick targets:")

    assert cb.call_args.args[0] == "Pick targets:"


def test_pick_container_label_includes_base_and_git_status(mocker):
    from jailbee.git_status import GitStatus

    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "myrepo-feat-a"

    containers = [
        ContainerInfo(
            name="myrepo-feat-a",
            state="Running",
            network="strict",
            ip="10.0.0.42",
            memory_limit="4GB",
            repo="myrepo",
            base_branch="main",
            git_status=GitStatus(wt="+1 -0", ahead_diff="+2 -0", ahead_count="1", conflict="ok"),
        ),
        ContainerInfo(
            name="myrepo-legacy",
            state="Stopped",
            network="strict",
            ip=None,
            memory_limit="4GB",
            repo="myrepo",
            base_branch=None,
            git_status=None,
        ),
    ]

    pick_container(containers)

    choices = select.call_args.kwargs["choices"]
    titles = [c.title for c in choices]
    assert "main" in titles[0]
    assert "+1 -0" in titles[0]
    assert "+2 -0" in titles[0]
    assert "—" in titles[1]  # legacy: dashes


def test_pick_containers_multi_label_includes_base_and_git_status(mocker):
    cb = mocker.patch("jailbee.tui.checkbox")
    cb.return_value = []

    containers = [
        ContainerInfo(
            name="myrepo-feat-a",
            state="Running",
            network="strict",
            ip="10.0.0.42",
            memory_limit="4GB",
            repo="myrepo",
            base_branch="develop",
            git_status=None,
        ),
    ]
    pick_containers_multi(containers)
    titles = [c.title for c in cb.call_args.kwargs["choices"]]
    assert "develop" in titles[0]


def test_choice_title_includes_conflict() -> None:
    from jailbee.git_status import GitStatus
    from jailbee.tui import _choice_widths, _format_choice_title

    c = ContainerInfo(
        name="myrepo-feat-a",
        state="Running",
        network="strict",
        ip="10.0.0.42",
        memory_limit="4GB",
        repo="myrepo",
        base_branch="dev",
        git_status=GitStatus(wt="clean", ahead_diff="+1 -0", ahead_count="1", conflict="conflict"),
    )
    widths = _choice_widths([c])
    title = _format_choice_title(c, widths)
    assert "conflict" in title


def test_claude_picker_lines_up_the_accounts_and_appends_the_org() -> None:
    """The picker mirrors `claude ls`'s split: the account column carries
    `display_name` (no `#<org8>` inside it) and the org follows, padded so the
    org column lines up across rows of differing email length."""
    from pathlib import Path

    from jailbee.claude_pool import Slot
    from jailbee.tui import _claude_choice_title

    long = Slot("tuomas.airaksinen@gisgro.com#d38d520c", Path("/s/a.json"), live=False)
    short = Slot("me@x.com#aaaabbbb", Path("/s/b.json"), live=False)
    width = max(len(s.display_name) for s in (long, short))

    long_title = _claude_choice_title(long, width)
    short_title = _claude_choice_title(short, width)
    assert long_title == "tuomas.airaksinen@gisgro.com  d38d520c"
    assert "#d38d520c" not in long_title
    # The short row's org starts at the same column as the long row's.
    assert short_title.index("aaaabbbb") == long_title.index("d38d520c")


def test_claude_picker_omits_the_org_for_an_account_without_one() -> None:
    from pathlib import Path

    from jailbee.claude_pool import Slot
    from jailbee.tui import _claude_choice_title

    plain = Slot("me@personal.com", Path("/s/b.json"), live=False)
    # Padded to a wider column, it would still carry no trailing whitespace:
    # nothing follows the account when there is no organization.
    assert _claude_choice_title(plain, 40) == "me@personal.com"


def test_claude_picker_offers_the_slot_name_as_the_value(mocker) -> None:
    """The picker's *value* must be the full slot name, not the shortened
    display text — the name is what `claude use`/`rm` resolve."""
    from pathlib import Path

    from jailbee.claude_pool import Slot
    from jailbee.tui import pick_claude_account

    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "me@corp.com#d38d520c"
    slots = [Slot("me@corp.com#d38d520c", Path("/s/a.json"), live=False)]

    result = pick_claude_account(slots, message="Switch this repo to:")

    assert result == "me@corp.com#d38d520c"
    assert [c.value for c in select.call_args.kwargs["choices"]] == ["me@corp.com#d38d520c"]
    assert select.call_args.args[0] == "Switch this repo to:"


def test_choice_widths_size_the_job_column_to_the_full_label(mocker) -> None:
    """The job column must widen for the '(worker gone)' suffix, not just the
    bare phase — otherwise `_format_choice_title` truncates the label it's
    asked to render."""
    from jailbee import background
    from jailbee.tui import _choice_widths, _format_choice_title

    mocker.patch.object(background, "worker_alive", return_value=False)
    c = ContainerInfo(
        name="myrepo-feat-a",
        state="—",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase="cloning",
        job_pid=4242,
        job_kind="create",
    )
    widths = _choice_widths([c])
    assert widths["job"] == len("cloning (worker gone)")
    title = _format_choice_title(c, widths)
    assert "cloning (worker gone)" in title


# --- checkbox() wrapper: drive a real prompt_toolkit Application via a pipe ---


@pytest.fixture
def _checkbox_io():
    """Yield (send, run) helpers wired to a fresh pipe input + dummy output."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:

        def run(keys: str, **kwargs):
            pipe_input.send_text(keys)
            return checkbox(
                "Pick:",
                choices=["a", "b", "c"],
                input=pipe_input,
                output=DummyOutput(),
                **kwargs,
            )

        yield run


def test_checkbox_bare_enter_returns_pointed_at(_checkbox_io):
    # No space toggles, just Enter on the first row.
    assert _checkbox_io("\r") == ["a"]


def test_checkbox_bare_enter_after_arrow_returns_highlighted(_checkbox_io):
    # Down-arrow once, then Enter — should pick the second row.
    assert _checkbox_io("\x1b[B\r") == ["b"]


def test_checkbox_space_then_enter_returns_only_toggled(_checkbox_io):
    # Toggle the first row, then Enter — bypasses the pointed-at fallback.
    assert _checkbox_io(" \r") == ["a"]


def test_checkbox_multiple_spaces_then_enter(_checkbox_io):
    # Space on row 1, arrow down, space on row 2, Enter.
    assert _checkbox_io(" \x1b[B \r") == ["a", "b"]


def test_checkbox_default_to_pointed_false_returns_empty(_checkbox_io):
    # Opt-out preserves upstream questionary behaviour.
    assert _checkbox_io("\r", default_to_pointed=False) == []


def test_pick_container_label_includes_job_phase(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "myrepo-feat-bg"

    ready = _info("myrepo-feat-ready")
    # No pid: only reachable from a hand-built ContainerInfo like this one,
    # so the label falls back to the bare phase (see job_label_or_empty).
    in_flight = ContainerInfo(
        name="myrepo-feat-bg",
        state="—",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase="cloning",
    )
    pick_container([ready, in_flight])

    choices = select.call_args.kwargs["choices"]
    assert "cloning" in choices[1].title
    # rows without an op stay clean (no stray "None")
    assert "None" not in choices[0].title


def test_pick_container_label_names_the_phase_a_dead_worker_died_in(mocker):
    """The picker must not disagree with `gie ls` / `gie job ls` about a dead
    worker: a bare 'cloning' here would wrongly imply waiting will work."""
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=False)
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "myrepo-feat-bg"

    ready = _info("myrepo-feat-ready")
    dead = ContainerInfo(
        name="myrepo-feat-bg",
        state="—",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase="cloning",
        job_pid=4242,
        job_kind="create",
    )
    pick_container([ready, dead])

    choices = select.call_args.kwargs["choices"]
    assert "cloning (worker gone)" in choices[1].title


def _plan(**overrides):
    from jailbee.sync import BridgePlan, RefSummary

    defaults = dict(
        direction="push",
        container_short="feat-foo",
        container_full="app-feat-foo",
        container_state="Running",
        source=RefSummary(label="origin/main", oid="a1b2c3d" + "0" * 33, subject="Bump deps"),
        target=RefSummary(label="feat/foo", oid="9f8e7d6" + "0" * 33, subject="WIP parser"),
        action="merge",
        incoming=4,
        notes=(),
    )
    defaults.update(overrides)
    return BridgePlan(**defaults)


def test_render_bridge_plan_push_block():
    from jailbee.tui import render_bridge_plan

    block = render_bridge_plan(_plan())

    assert "Push  host ──▶ container" in block
    assert "container : feat-foo  (app-feat-foo, Running)" in block
    assert 'source    : origin/main  a1b2c3d "Bump deps"' in block
    assert 'target    : feat/foo     9f8e7d6 "WIP parser"' in block
    assert "4 commit(s) to apply" in block
    assert "action    : merge" in block


def test_render_bridge_plan_pull_direction_is_reversed():
    from jailbee.tui import render_bridge_plan

    block = render_bridge_plan(_plan(direction="pull", action="merge"))

    assert "Pull  container ──▶ host" in block


def test_render_bridge_plan_omits_unavailable_fields():
    from jailbee.sync import RefSummary
    from jailbee.tui import render_bridge_plan

    block = render_bridge_plan(
        _plan(
            target=RefSummary(label="develop", oid=None, subject=None),
            incoming=None,
        )
    )

    assert "target    : develop" in block
    assert "commit(s) to apply" not in block
    assert "None" not in block


def test_render_bridge_plan_renders_notes():
    from jailbee.tui import render_bridge_plan

    block = render_bridge_plan(_plan(notes=("container working tree is dirty",)))

    assert "⚠ container working tree is dirty" in block


def test_default_confirm_delegates_to_rich(mocker):
    from jailbee import tui

    ask = mocker.patch("rich.prompt.Confirm.ask", return_value=True)
    assert tui.default_confirm("Proceed?") is True
    ask.assert_called_once_with("Proceed?", default=False)


def test_default_confirm_returns_false_without_stdin(mocker):
    """`gie new < /dev/null` declines instead of raising.

    With no stdin there is no answer to be had, and callers turn `False` into
    the "declined — pass --yes" abort, which is a better message than Click's
    bare "Aborted!".
    """
    from jailbee import tui

    mocker.patch("rich.prompt.Confirm.ask", side_effect=EOFError("EOF when reading a line"))
    assert tui.default_confirm("Proceed?") is False


def test_default_confirm_lets_ctrl_c_propagate(mocker):
    """Ctrl-C means "abandon the command", not "answer no".

    The distinction matters for `run_apply`, which treats a `False` answer as
    "skip the restart" and then finishes normally — so swallowing
    KeyboardInterrupt here would make `gie apply` exit 0 having done half its
    job. Click turns the escaping exception into `Abort`.
    """
    import pytest

    from jailbee import tui

    mocker.patch("rich.prompt.Confirm.ask", side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        tui.default_confirm("Proceed?")


def test_confirm_fn_alias_is_str_to_bool():
    """Pins the injectable-confirmation seam's shape: every `confirm_fn`
    parameter (`apply.run_apply`, `lifecycle.new_container`) is annotated with
    this alias, and `default_confirm` must remain assignable to it.
    """
    from collections.abc import Callable

    from jailbee.tui import ConfirmFn

    assert ConfirmFn == Callable[[str], bool]


def test_warn_plain_keeps_bracketed_text_verbatim(capsys):
    """The hazard `warn_plain` exists for: Rich reads `[wip]` as a style tag.

    Real module-level Console, no mocking — the contrast assertion below on
    `warn` is what makes the difference load-bearing rather than incidental.
    """
    from jailbee import tui

    tui.warn_plain("branch feat/[wip] step on_create[build]")
    plain = capsys.readouterr().out
    assert "feat/[wip]" in plain
    assert "on_create[build]" in plain
    assert plain.startswith("⚠ ")

    tui.warn("branch feat/[wip] step on_create[build]")
    marked_up = capsys.readouterr().out
    assert "[wip]" not in marked_up  # silently deleted as a style tag
    assert "[build]" not in marked_up


def test_error_plain_keeps_bracketed_text_verbatim(capsys):
    """Same hazard as `warn_plain`, on stderr.

    The two cases in the tree: a pip extra in the missing-PySide6 hint, and
    the `[<container>]` prefix on a batch-push failure. Under plain `error`
    both lose the brackets and everything between them.
    """
    from jailbee import tui

    tui.error_plain("uv tool install 'jailbee[gui]' for [feat-wip]")
    plain = capsys.readouterr().err
    assert "jailbee[gui]" in plain
    assert "[feat-wip]" in plain
    assert plain.startswith("✗ ")

    tui.error("uv tool install 'jailbee[gui]' for [feat-wip]")
    marked_up = capsys.readouterr().err
    assert "[gui]" not in marked_up  # silently deleted as a style tag
    assert "[feat-wip]" not in marked_up


def test_render_bridge_plan_returns_a_plain_string():
    """`render_bridge_plan` returns plain text — no Rich markup interpretation
    happens here regardless of the input. That's true of any implementation,
    so it isn't the interesting property; the interesting property is that
    the *caller* (`cli._confirm_bridge_plan`) must print this with
    `markup=False` so Rich doesn't reinterpret a branch name like
    'feat/[wip]' as a tag and silently swallow it. That call-site behavior is
    covered end-to-end (real Console, no mocking) by
    test_cli_checkout_confirm.py::test_checkout_plan_block_prints_bracketed_branch_names_verbatim.
    """
    from jailbee.sync import RefSummary
    from jailbee.tui import render_bridge_plan

    block = render_bridge_plan(_plan(source=RefSummary(label="feat/[wip]", oid=None, subject=None)))

    assert "feat/[wip]" in block


# ---- status_with_elapsed: a spinner that also answers "how long?" ----


def test_format_elapsed_switches_to_minutes():
    from jailbee.tui import format_elapsed

    assert format_elapsed(9.2) == "9s"
    assert format_elapsed(59.9) == "59s"
    assert format_elapsed(90.4) == "1m30s"
    assert format_elapsed(605) == "10m05s"


def test_elapsed_status_hides_the_counter_until_it_means_something(mocker):
    """A step that finishes in a second should not flash "0s" at anyone."""
    from jailbee.tui import ElapsedStatus

    # A synthetic clock, patched before construction, rather than deriving the
    # fake "now" from the real one as `handle._started + 42`. monotonic()
    # returns seconds since boot, and for many real values (T + 42) - T lands
    # just under 42 in float64 — 31.416816438270224 gives 41.99999999999999 —
    # which format_elapsed renders as "41s", truncating by design. How often
    # depends on the magnitude of T, so the old arithmetic failed ~13% of runs
    # on a freshly booted CI runner and effectively never on a dev box with
    # days of uptime. 1000.0 and 1042.0 subtract exactly.
    monotonic = mocker.patch("jailbee.tui.time.monotonic", return_value=1000.0)
    status = mocker.MagicMock()
    handle = ElapsedStatus(status, "provisioning")

    handle.refresh()
    assert status.update.call_args[0][0] == "⏳ provisioning…"

    monotonic.return_value = 1042.0
    handle.refresh()
    assert status.update.call_args[0][0] == "⏳ provisioning… — 42s"


def test_elapsed_status_restarts_the_clock_on_a_new_step(mocker):
    """The number must answer "how long has *this* step run", not "how long
    since the command started" — otherwise a five-step command shows one
    ever-growing figure that says nothing about where it is stuck."""
    from jailbee.tui import ElapsedStatus

    status = mocker.MagicMock()
    monotonic = mocker.patch("jailbee.tui.time.monotonic", return_value=1000.0)
    handle = ElapsedStatus(status, "first step")

    monotonic.return_value = 1300.0  # five minutes into the first step
    handle.update("second step")

    assert status.update.call_args[0][0] == "⏳ second step…"  # no stale 5m00s
    monotonic.return_value = 1310.0
    handle.refresh()
    assert status.update.call_args[0][0] == "⏳ second step… — 10s"


def test_status_with_elapsed_stops_its_ticker_on_exit(mocker):
    """The ticker is a thread; leaking one per invocation would keep the
    process alive past the command in a long-running host process."""
    import threading

    from jailbee.tui import status_with_elapsed

    mocker.patch("jailbee.tui.console.status")
    before = threading.active_count()

    with status_with_elapsed("working") as handle:
        handle.update("still working")

    assert threading.active_count() == before


def test_choose_shared_credential_returns_none_without_a_tty(mocker):
    """`jailbee apply` in CI or under a pipe must refuse, not hang on stdin."""
    from pathlib import Path

    from jailbee.tui import choose_shared_credential

    mocker.patch("jailbee.tui.sys.stdin.isatty", return_value=False)
    select = mocker.patch("questionary.select")

    assert choose_shared_credential(Path("/creds/work"), Path("/shared/claude"), "app") is None
    select.assert_not_called()


def test_choose_shared_credential_offers_both_sides_and_names_the_opt_out(mocker, capsys):
    """The two credential choices plus the `repos:` route out of the group —
    without the hint, a user who wants neither has no visible third option."""
    from pathlib import Path

    from jailbee.tui import choose_shared_credential

    mocker.patch("jailbee.tui.sys.stdin.isatty", return_value=True)
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "group"

    result = choose_shared_credential(
        Path("/creds/work"), Path("/shared/claude/.credentials.json"), "SampleApp"
    )

    assert result == "group"
    values = [c.value for c in select.call_args.kwargs["choices"]]
    assert values == ["group", "repo", "cancel"]
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "claude_credentials" in combined
    assert "global.yaml" in combined
    # The block must be copy-pasteable: `repos` is keyed by container_prefix,
    # and a placeholder there is the one part the user cannot fill in from
    # the prompt alone.
    assert "SampleApp: null" in combined


@pytest.mark.parametrize("answer", [None, "cancel"])
def test_choose_shared_credential_maps_both_cancel_answers_to_none(mocker, answer):
    """questionary returns None on Ctrl-C and the sentinel on the `cancel`
    row; a `value=None` Choice would instead hand back its own title."""
    from pathlib import Path

    from jailbee.tui import choose_shared_credential

    mocker.patch("jailbee.tui.sys.stdin.isatty", return_value=True)
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = answer

    assert (
        choose_shared_credential(Path("/creds/work"), Path("/x/.credentials.json"), "app") is None
    )
