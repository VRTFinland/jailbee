"""CLI tests for the `jailbee issue` group.

Fully mocked: `issue_outbox`'s container reads and every orchestration call
(`prepare_batch`, `plan_lines`, `revalidate_batch`, `apply_batch`,
`reconcile_action`) are patched, so nothing here touches
Incus or GitHub. The TTY check is patched the same way `test_cli_review.py`
patches it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.issue_outbox import (
    OutboxSnapshot,
    RepoTarget,
)
from jailbee.outbox_io import ContainerIdentity, IssueJournal, JournalAction
from tests.conftest import panel_text

runner = CliRunner()

_IDENTITY = ContainerIdentity(full_name="acme-feat-foo", created_at="2026-01-01T00:00:00Z")
_DIGEST = "a" * 64
_UUID = "uuid-feat-foo"


def _manifest_text(**overrides) -> str:
    payload = {
        "version": 1,
        "actions": [{"type": "comment", "repo": ".", "issue": 42, "body": "looks good"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _stub_journals(mocker, journal):
    """Every staged manifest has `journal` as its store journal (None: no journals)."""

    def journals(identity, uuid):
        from jailbee import issue_outbox

        if journal is None:
            return {}
        return {name: journal for name in issue_outbox.store_outbox(uuid).manifest_names}

    mocker.patch("jailbee.issue_outbox.store_journals", side_effect=journals)


def _setup(mocker, tmp_path, *, files=None):
    cfg = mocker.MagicMock()
    cfg.repo_root = tmp_path
    cfg.container_prefix = "acme"
    cfg.upstream_remote = "origin"
    cfg.container_user.uid = 1000
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "acme-feat-foo"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    snapshot = OutboxSnapshot(files=files or {})
    # `apply`, `show` and `resolve` read proposals and journals from the host
    # outbox store by instance uuid.
    mocker.patch("jailbee.issue_outbox.store_outbox", return_value=snapshot)
    mocker.patch("jailbee.outbox.service.instance_identity", return_value=(_IDENTITY, _UUID))

    _stub_journals(mocker, None)
    return cfg, incus


# ---- registration / help ----------------------------------------------------


def test_issue_help_lists_the_visible_subcommands():
    import typer.main

    group = typer.main.get_command(app).commands["issue"]  # type: ignore[attr-defined]  # click Group

    for name in ("apply", "resolve"):
        assert not group.commands[name].hidden
    # Listing, showing and dropping issue proposals is `outbox ls|show|drop`.
    for gone in ("ls", "show", "drop"):
        assert gone not in group.commands


def test_apply_help_shows_its_options():
    result = runner.invoke(app, ["issue", "apply", "--help"])

    assert result.exit_code == 0, result.output
    # The positional's own row, not the docstring's mention of a manifest.
    assert "[name] [manifest]" in result.output
    assert "Manifest file name" in result.output
    assert "--yes" in result.output
    assert "--dry-run" in result.output


def test_resolve_help_shows_its_options():
    result = runner.invoke(app, ["issue", "resolve", "--help"])

    assert result.exit_code == 0, result.output
    for opt in ("--applied", "--url", "--issue", "--retry", "--yes"):
        assert opt in result.output


def _setup_resolve(mocker, tmp_path, *, manifest_text=None, journal_actions=()):
    _setup(mocker, tmp_path, files={"001.json": manifest_text or _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    journal = IssueJournal(
        identity=_IDENTITY,
        manifest_name="001.json",
        digest=_DIGEST,
        action_count=1,
        actions=journal_actions,
    )
    _stub_journals(mocker, journal if journal_actions else None)
    mocker.patch(
        "jailbee.issue_outbox.resolve_repo_targets",
        return_value={".": RepoTarget(".", tmp_path, "acme/widgets")},
    )


def test_resolve_neither_flag_off_a_tty_exits_2(mocker, tmp_path):
    """Off a terminal the missing resolution is an error naming both flags."""
    _setup_resolve(mocker, tmp_path)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "0"])

    assert result.exit_code == 2
    assert "--applied" in panel_text(result.output) and "--retry" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_rejects_both_applied_and_retry(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path)
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(
        app,
        ["issue", "resolve", "feat-foo", "001.json", "0", "--applied", "--url", "x", "--retry"],
    )

    assert result.exit_code == 2
    assert "Exactly one of" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_validates_create_only_issue_option(mocker, tmp_path):
    """--issue is only meaningful for a create action."""
    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "hello"}])
    _setup_resolve(mocker, tmp_path, manifest_text=text)

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            "feat-foo",
            "001.json",
            "0",
            "--applied",
            "--url",
            "https://github.com/acme/widgets/issues/42",
            "--issue",
            "42",
        ],
    )

    assert result.exit_code == 2
    assert "--issue" in result.output


def test_resolve_create_action_requires_issue_off_a_tty(mocker, tmp_path):
    text = json.dumps(
        {
            "version": 1,
            "actions": [
                {
                    "type": "create",
                    "repo": ".",
                    "ref": "r1",
                    "title": "t",
                    "body": "b",
                    "labels": [],
                }
            ],
        }
    )
    _setup_resolve(mocker, tmp_path, manifest_text=text)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            "feat-foo",
            "001.json",
            "0",
            "--applied",
            "--url",
            "https://github.com/acme/widgets/issues/99",
        ],
    )

    assert result.exit_code == 2
    assert "issue number" in panel_text(result.output)


def test_resolve_renders_the_action_before_confirming(mocker, tmp_path, typed_confirmations):
    text = _manifest_text(
        actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "the pending comment"}]
    )
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="unclear"),
        ),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            "feat-foo",
            "001.json",
            "0",
            "--applied",
            "--url",
            "https://github.com/acme/widgets/issues/42",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "the pending comment" in result.output
    reconcile.assert_called_once()
    kwargs = reconcile.call_args.kwargs
    assert kwargs["index"] == 0
    from jailbee.issue_outbox import AppliedResolution

    assert kwargs["resolution"] == AppliedResolution(
        url="https://github.com/acme/widgets/issues/42", issue=None
    )


def test_resolve_retry_forgets_the_uncertain_action(mocker, tmp_path):
    text = _manifest_text(
        actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "the pending comment"}]
    )
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="unclear"),
        ),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "0", "--retry", "-y"])

    assert result.exit_code == 0, result.output
    reconcile.assert_called_once()
    from jailbee.issue_outbox import RetryResolution

    assert reconcile.call_args.kwargs["resolution"] == RetryResolution()


def test_resolve_requires_a_prompt_or_yes_off_tty(mocker, tmp_path):
    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "x"}])
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="unclear"),
        ),
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            "feat-foo",
            "001.json",
            "0",
            "--applied",
            "--url",
            "https://github.com/acme/widgets/issues/42",
        ],
    )

    assert result.exit_code == 2
    reconcile.assert_not_called()


def test_resolve_reports_a_journal_error_and_exits_1(mocker, tmp_path):
    from jailbee.outbox_io import JournalError

    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "x"}])
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="unclear"),
        ),
    )
    mocker.patch(
        "jailbee.issue_outbox.reconcile_action",
        side_effect=JournalError("receipt URL does not match this action's issue"),
    )

    result = runner.invoke(
        app,
        [
            "issue",
            "resolve",
            "feat-foo",
            "001.json",
            "0",
            "--applied",
            "--url",
            "https://github.com/acme/widgets/issues/1",
            "-y",
        ],
    )

    assert result.exit_code == 1
    assert "does not match" in result.output


# ---- interactive missing values ---------------------------------------------


def _uncertain_setup(mocker, tmp_path, *, name_resolver=True):
    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "b"}])
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="?"),
        ),
    )
    if name_resolver:
        mocker.patch(
            "jailbee.cli._resolve_issue_container",
            return_value=(mocker.MagicMock(), "acme-feat-foo"),
        )
    return mocker.patch("jailbee.issue_outbox.reconcile_action")


def test_resolve_asks_manifest_action_mode_and_url(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    select = mocker.patch("jailbee.prompting._select", side_effect=["001.json", 0, "applied"])
    mocker.patch(
        "jailbee.prompting._ask", return_value="https://github.com/acme/widgets/issues/42#c1"
    )

    result = runner.invoke(app, ["issue", "resolve", "-y"])

    assert result.exit_code == 0, result.output
    assert select.call_count == 3  # manifest, action (one each, destructive), mode
    from jailbee.issue_outbox import AppliedResolution

    assert reconcile.call_args.kwargs["resolution"] == AppliedResolution(
        url="https://github.com/acme/widgets/issues/42#c1", issue=None
    )


def test_resolve_asks_for_the_created_issue_number(mocker, tmp_path):
    text = json.dumps(
        {
            "version": 1,
            "actions": [
                {
                    "type": "create",
                    "repo": ".",
                    "ref": "r1",
                    "title": "t",
                    "body": "b",
                    "labels": [],
                }
            ],
        }
    )
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="?"),
        ),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")
    mocker.patch("jailbee.prompting._ask", side_effect=["99"])

    result = runner.invoke(
        app,
        [
            "issue", "resolve", "feat-foo", "001.json", "0", "--applied",
            "--url", "https://github.com/acme/widgets/issues/99", "-y",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert reconcile.call_args.kwargs["resolution"].issue == 99


def test_resolve_without_uncertain_manifests_exits_2(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path)  # no journal actions: nothing uncertain
    mocker.patch(
        "jailbee.cli._resolve_issue_container",
        return_value=(mocker.MagicMock(), "acme-feat-foo"),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(app, ["issue", "resolve"])

    assert result.exit_code == 2
    assert "nothing uncertain" in panel_text(result.output).lower()
    reconcile.assert_not_called()


def test_resolve_without_an_uncertain_action_exits_2(mocker, tmp_path):
    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "b"}])
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=text,
        journal_actions=(JournalAction(index=0, state="applied", repo="acme/widgets", detail=""),),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "--retry", "-y"])

    assert result.exit_code == 2
    assert "no uncertain action" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_cancel_at_the_manifest_picker_exits_1(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting._select", return_value=None)

    result = runner.invoke(app, ["issue", "resolve", "-y"])

    assert result.exit_code == 1
    reconcile.assert_not_called()


def test_resolve_cancel_at_the_action_picker_exits_1(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting._select", side_effect=["001.json", None])

    result = runner.invoke(app, ["issue", "resolve", "-y"])

    assert result.exit_code == 1
    reconcile.assert_not_called()


def test_resolve_cancel_at_the_mode_picker_exits_1(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting._select", side_effect=["001.json", 0, None])

    result = runner.invoke(app, ["issue", "resolve", "-y"])

    assert result.exit_code == 1
    reconcile.assert_not_called()


def test_resolve_manifest_off_a_tty_exits_2_without_side_effect(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "--retry", "-y"])

    assert result.exit_code == 2
    assert "001.json" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_mode_off_a_tty_still_requires_a_flag(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "0"])

    assert result.exit_code == 2
    assert "--applied" in panel_text(result.output) and "--retry" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_retry_still_rejects_url(mocker, tmp_path):
    reconcile = _uncertain_setup(mocker, tmp_path)

    result = runner.invoke(
        app,
        ["issue", "resolve", "feat-foo", "001.json", "0", "--retry", "--url", "https://x", "-y"],
    )

    assert result.exit_code == 2
    assert "--retry accepts neither" in panel_text(result.output)
    reconcile.assert_not_called()


_CREATE_TEXT = json.dumps(
    {
        "version": 1,
        "actions": [
            {
                "type": "create",
                "repo": ".",
                "ref": "r1",
                "title": "t",
                "body": "b",
                "labels": [],
            }
        ],
    }
)


def test_resolve_applied_without_url_off_a_tty_names_the_flag(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "0", "--applied"])

    assert result.exit_code == 2
    assert "--url" in panel_text(result.output)
    reconcile.assert_not_called()


def test_resolve_create_without_issue_off_a_tty_names_the_flag(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path, manifest_text=_CREATE_TEXT)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)

    result = runner.invoke(
        app,
        [
            "issue", "resolve", "feat-foo", "001.json", "0", "--applied",
            "--url", "https://github.com/acme/widgets/issues/99",
        ],
    )  # fmt: skip

    assert result.exit_code == 2
    assert "--issue" in panel_text(result.output)


@pytest.mark.parametrize("bad", ["²", "0", "-3", "x"])
def test_resolve_created_issue_number_rejects_non_positive_and_non_ascii(mocker, tmp_path, bad):
    _setup_resolve(
        mocker,
        tmp_path,
        manifest_text=_CREATE_TEXT,
        journal_actions=(
            JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="?"),
        ),
    )
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")
    ask = mocker.patch("jailbee.prompting._ask", side_effect=[bad, "7"])

    result = runner.invoke(
        app,
        [
            "issue", "resolve", "feat-foo", "001.json", "0", "--applied",
            "--url", "https://github.com/acme/widgets/issues/7", "-y",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert ask.call_count == 2
    assert reconcile.call_args.kwargs["resolution"].issue == 7


def test_resolve_rejects_issue_on_a_non_create_before_asking_for_the_url(mocker, tmp_path):
    text = _manifest_text(actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "hello"}])
    _setup_resolve(mocker, tmp_path, manifest_text=text)
    ask = mocker.patch("jailbee.prompting._ask")
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)

    result = runner.invoke(
        app, ["issue", "resolve", "feat-foo", "001.json", "0", "--applied", "--issue", "42"]
    )

    assert result.exit_code == 2
    assert "--issue is only valid" in panel_text(result.output)
    ask.assert_not_called()
