"""CLI tests for the `gie git checkout` confirmation block."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ResolvedContainer


def _fake_checkout_plan():
    from jailbee.sync import BridgePlan, RefSummary

    return BridgePlan(
        direction="checkout",
        container_short="feat-only",
        container_full="myrepo-feat-only",
        container_state="Running",
        source=RefSummary(label="feat/foo", oid="a" * 40, subject="WIP parser"),
        target=RefSummary(label="feat/foo", oid=None, subject=None),
        action="ff-only",
        incoming=3,
        notes=("'feat/foo' will be created on the host",),
    )


def _wire(mocker, tmp_path, *, auto_selected: bool, auto_target: bool = True):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "myrepo"
    cfg_mock.confirm.auto_target = auto_target
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive_detailed",
        return_value=ResolvedContainer(name="myrepo-feat-only", auto_selected=auto_selected),
    )
    mocker.patch(
        "jailbee.lifecycle.short_name",
        side_effect=lambda _cfg, full: full.removeprefix("myrepo-"),
    )
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    # checkout_from_container is mocked per test, so its result is a MagicMock;
    # keep the post-operation summary out of the way rather than feeding mock
    # attributes into the fetch-summary formatter.
    mocker.patch("jailbee.cli._print_fetch_summary")
    mocker.patch("jailbee.cli.success")
    return cfg_mock


def test_checkout_confirms_an_auto_selected_container(mocker, tmp_path):
    _wire(mocker, tmp_path, auto_selected=True)
    mocker.patch("jailbee.sync.plan_checkout", return_value=_fake_checkout_plan())
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"], input="y\n")

    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Checkout  container ──▶ host" in combined
    assert "will be created on the host" in combined
    checkout.assert_called_once()


def test_checkout_declined_confirmation_does_nothing(mocker, tmp_path):
    _wire(mocker, tmp_path, auto_selected=True)
    mocker.patch("jailbee.sync.plan_checkout", return_value=_fake_checkout_plan())
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"], input="n\n")

    assert result.exit_code != 0
    checkout.assert_not_called()


def test_checkout_with_an_explicit_name_is_not_confirmed(mocker, tmp_path):
    _wire(mocker, tmp_path, auto_selected=False)
    plan_checkout = mocker.patch("jailbee.sync.plan_checkout")
    mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout", "feat-only"])

    assert result.exit_code == 0
    plan_checkout.assert_not_called()


def test_checkout_no_confirm_flag_skips_the_plan(mocker, tmp_path):
    _wire(mocker, tmp_path, auto_selected=True)
    plan_checkout = mocker.patch("jailbee.sync.plan_checkout")
    mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout", "--no-confirm"])

    assert result.exit_code == 0
    plan_checkout.assert_not_called()


def test_checkout_confirm_flag_overrides_a_disabled_config(mocker, tmp_path):
    _wire(mocker, tmp_path, auto_selected=True, auto_target=False)
    mocker.patch("jailbee.sync.plan_checkout", return_value=_fake_checkout_plan())
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout", "--confirm"], input="y\n")

    assert result.exit_code == 0
    assert "Checkout  container ──▶ host" in (result.stdout + (result.stderr or ""))
    checkout.assert_called_once()


def test_checkout_unbuildable_plan_skips_the_prompt_and_still_checks_out(mocker, tmp_path):
    """A plan is a preview: an IncusError while building one must not fail
    the command — the checkout proceeds and produces its own precise error.
    """
    from jailbee.incus import IncusError

    _wire(mocker, tmp_path, auto_selected=True)
    mocker.patch("jailbee.sync.plan_checkout", side_effect=IncusError("incus list failed"))
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"])

    assert result.exit_code == 0
    assert "Checkout  container ──▶ host" not in (result.stdout + (result.stderr or ""))
    checkout.assert_called_once()


def test_checkout_unbuildable_plan_value_error_skips_the_prompt_and_still_checks_out(
    mocker, tmp_path
):
    """Same tolerance, the other caught exception: resolve_container_name
    raises ValueError for a container that vanished between listing and now.
    """
    _wire(mocker, tmp_path, auto_selected=True)
    mocker.patch("jailbee.sync.plan_checkout", side_effect=ValueError("no such container"))
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"])

    assert result.exit_code == 0
    assert "Checkout  container ──▶ host" not in (result.stdout + (result.stderr or ""))
    checkout.assert_called_once()


def test_checkout_off_tty_prints_the_plan_and_proceeds(mocker, tmp_path):
    """`checkout` auto-selects a single container regardless of TTY (unlike
    `push`, which requires a TTY before it ever lists containers off one).
    No prompt is possible off a TTY; the block still lands in the log.
    """
    _wire(mocker, tmp_path, auto_selected=True)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch("jailbee.sync.plan_checkout", return_value=_fake_checkout_plan())
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"])

    assert result.exit_code == 0
    assert "Checkout  container ──▶ host" in (result.stdout + (result.stderr or ""))
    checkout.assert_called_once()


def test_checkout_plan_block_prints_bracketed_branch_names_verbatim(mocker, tmp_path):
    """Branch names may contain '[' (e.g. 'feat/[wip]'). `cli._confirm_bridge_plan`
    prints the block with `markup=False` specifically so Rich doesn't
    reinterpret that as a markup tag and silently swallow it. This test does
    NOT mock `console.print`, so it exercises that call site for real: with
    the default `markup=True`, Rich renders 'feat/[wip]' as 'feat/' (the
    unrecognised tag vanishes, taking the rest of the label with it).
    """
    import dataclasses

    from jailbee.sync import RefSummary

    _wire(mocker, tmp_path, auto_selected=True)
    bracket_plan = dataclasses.replace(
        _fake_checkout_plan(),
        source=RefSummary(label="feat/[wip]", oid="a" * 40, subject="WIP"),
    )
    mocker.patch("jailbee.sync.plan_checkout", return_value=bracket_plan)
    mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"], input="y\n")

    assert result.exit_code == 0
    assert "feat/[wip]" in (result.stdout + (result.stderr or ""))


def test_checkout_mount_mode_auto_selected_skips_the_confirmation(mocker, tmp_path):
    """M5: an auto-selected mount-mode container gets no plan block and no
    [Y/n] — fetch/checkout/merge don't apply in mount mode
    (`assert_container_publishable` refuses outright), so asking is asking
    to confirm an operation that is guaranteed to fail.
    """
    _wire(mocker, tmp_path, auto_selected=True)
    incus_cls = mocker.patch("jailbee.incus.Incus")
    incus_cls.return_value.config_get.return_value = "mount"
    plan_checkout = mocker.patch("jailbee.sync.plan_checkout")
    checkout = mocker.patch("jailbee.sync.checkout_from_container")

    result = CliRunner().invoke(app, ["git", "checkout"])

    assert result.exit_code == 0
    plan_checkout.assert_not_called()
    checkout.assert_called_once()
