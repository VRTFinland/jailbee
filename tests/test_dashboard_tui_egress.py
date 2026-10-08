"""The Egress panel on Pilot: add, remove, cancel, policy re-checks and failures."""

from __future__ import annotations

import pytest

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.egress_scope import EntryRow
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import container_egress_keys, drive, keys, patch_pause


def test_egress_add_prompts_inline_then_runs_the_scoped_cli(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    rows = mocker.patch.object(tsession, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [*container_egress_keys(group), "a", *keys("example.com:8443"), "enter", "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    assert rows.call_count == 1  # a change that worked closes the panel: no reload
    assert rows.call_args_list[0].args[0::2] == (tmp_path, "alpha-x")
    prompt.assert_not_called()
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "add", "example.com:8443", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )
    asked = [
        view
        for view in run.trace
        if isinstance(view.overlay, tsession.TextPrompt) and view.overlay.purpose == "egress-add"
    ]
    assert asked, "the destination question was never drawn in the frame"
    # While the question is open the cursor stays on the container the panel
    # is about, not the repo header.
    assert asked[0].selected == dmodel.Row("container", "alpha-x")
    # After the submit the whole stack is gone — panel and the menu behind it —
    # so the next key acts on the table, not on a stale Esc chain.
    assert run.last.overlay is None


def test_ssh_egress_container_panel_can_remove_but_not_add_without_network(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"), restrict_host=True)
    mocker.patch.object(
        tsession, "load_egress_rows", return_value=(EntryRow("allowed.example", "config"),)
    )
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_egress_keys(group, remote=True, over_ssh=True, ssh_policy=policy),
        "a",
        "escape",
        "ctrl+c",
    ]
    run = drive(mocker, steps, groups=[group], remote=True, over_ssh=True, ssh_policy=policy)
    assert run.rc == 0

    prompt.assert_not_called()
    child.assert_not_called()
    egress = next(panel for panel in run.overlays() if isinstance(panel, tsession.EgressState))
    assert egress.can_add is False
    assert egress.can_rm is True
    # A refused add never opens the destination question.
    assert not run.of_type(tsession.TextPrompt)
    assert any("net egress add is not permitted" in str(notice) for notice in run.notices())


def test_run_removes_only_selected_container_override(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    rows = (
        EntryRow("from-config.example", "config"),
        EntryRow("container-only.example", "container"),
    )
    load = mocker.patch.object(tsession, "load_egress_rows", return_value=rows)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [*container_egress_keys(group), "j", "r", "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    assert load.call_count == 1  # a removal that worked closes the panel: no reload
    assert run.last.overlay is None
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "rm", "container-only.example", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )


def test_repo_egress_dispatch_uses_repo_scope_and_explicit_config(mocker, tmp_path):
    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dmodel.RepoGroup("alpha", str(tmp_path), config_path, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    steps = [
        "enter",
        *["j"] * 4,  # past New container…, New from PR…, Credential group…, Accounts…
        "enter",
        "enter",
        "a",
        *keys("repo.example:443"),
        "enter",
        "ctrl+c",
    ]
    assert drive(mocker, steps, groups=[group]).rc == 0

    prompt.assert_not_called()
    child.assert_called_once_with(
        [
            "jailbee",
            "net",
            "egress",
            "add",
            "repo.example:443",
            "--repo",
            "--config",
            str(config_path),
        ],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("cancel", ["escape", "ctrl+c"])
def test_egress_add_escape_returns_to_the_panel_with_a_notice(mocker, tmp_path, cancel):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_egress_keys(group), "a", *keys("x"), cancel, "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    prompt.assert_not_called()
    child.assert_not_called()
    assert any(n.text == "x" for n in run.prompts()), (
        "the typed text never reached the inline prompt"
    )
    # Cancelling answers the question, not the dashboard: the panel is back.
    last = run.last.overlay
    assert isinstance(last, tsession.EgressState)
    assert last.container == "alpha-x"
    assert run.last.notice == "Egress change cancelled"


def test_egress_add_rechecks_the_ssh_policy_at_submit(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    # `net egress add` is a host command: only reachable with restrict_host off.
    policy = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["net egress ls", "net egress add"]),
        restrict_host=False,
    )
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    child = mocker.patch.object(tsession.subprocess, "run")

    def revoke_add(_app) -> None:
        # The operator narrows the policy while the question is open.
        policy.commands.allow[:] = ["net egress ls"]

    steps = [
        *container_egress_keys(group, remote=True, over_ssh=True, ssh_policy=policy),
        "a",
        *keys("example.co"),
        revoke_add,
        "m",
        "enter",
    ]
    run = drive(mocker, steps, groups=[group], remote=True, over_ssh=True, ssh_policy=policy)
    assert run.rc == 0

    assert any(n.text == "example.com" for n in run.prompts()), (
        "the add question never opened under the permissive policy"
    )
    child.assert_not_called()
    assert "net egress add is not permitted by the SSH policy" in run.notices()
    assert isinstance(run.last.overlay, tsession.EgressState)


def test_egress_add_blank_destination_is_rejected_inline(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_egress_keys(group), "a", *keys("  "), "enter", "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    child.assert_not_called()
    # Enter keeps the question open with the reason (the trailing Ctrl-C
    # then cancels it, so this is not the last frame).
    rejected = [n for n in run.prompts() if n.error is not None]
    assert [n.error for n in rejected] == [
        "Destination (host, host:port, *.domain, IPv4, or CIDR) cannot be empty"
    ]


@pytest.mark.parametrize("returncode", [1, 2], ids=["mutation-failure", "invalid-destination"])
def test_egress_mutation_failure_is_visible(mocker, tmp_path, returncode):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = returncode

    steps = [*container_egress_keys(group), "a", *keys("invalid..example"), "enter", "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    # Destination validation stays the CLI's: the dashboard passes it through.
    child.assert_called_once_with(
        ["jailbee", "net", "egress", "add", "invalid..example", "alpha-x"],
        check=False,
        cwd=tmp_path,
    )
    assert any(f"exited {returncode}" in str(notice or "") for notice in run.notices())


def test_egress_panel_closes_when_container_disappears(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])

    def remove_container_while_typing(_app) -> None:
        # The container goes away after the question opened, before Enter.
        group.containers.clear()

    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_egress_keys(group),
        "a",
        *keys("example.com"),
        remove_container_while_typing,
        "x",
        "enter",
    ]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    child.assert_not_called()
    assert any(n.text == "example.comx" for n in run.prompts()), (
        "the prompt must still be open, with the text typed after the removal"
    )
    assert not isinstance(run.last.overlay, (tsession.EgressState, tsession.TextPrompt))
    assert "Egress target is no longer available" in run.notices()


def test_egress_panel_closes_when_repo_disappears_during_dispatch(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    child = mocker.patch.object(tsession.subprocess, "run", side_effect=FileNotFoundError())

    steps = [*container_egress_keys(group), "a", *keys("example.com"), "enter", "ctrl+c"]
    run = drive(mocker, steps, groups=[group])
    assert run.rc == 0

    child.assert_called_once()
    assert child.call_args.args[0] == ["jailbee", "net", "egress", "add", "example.com", "alpha-x"]
    assert not isinstance(run.last.overlay, tsession.EgressState)
    assert any("no longer exists" in str(notice or "") for notice in run.notices())


def test_egress_loader_failure_is_visible_and_does_not_crash(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(
        tsession, "load_egress_rows", side_effect=LookupError("database unavailable")
    )

    run = drive(mocker, [*container_egress_keys(group), "ctrl+c"], groups=[group])
    assert run.rc == 0

    assert any("could not load egress entries" in str(notice) for notice in run.notices())
