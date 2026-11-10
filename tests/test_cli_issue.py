"""CLI tests for the `jailbee issue` group.

Fully mocked: `issue_outbox`'s container reads and every orchestration call
(`prepare_batch`, `plan_lines`, `revalidate_batch`, `apply_batch`,
`reconcile_action`) are patched, so nothing here touches
Incus or GitHub. The TTY check is patched the same way `test_cli_review.py`
patches it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.issue_manifest import ExistingIssue, IssueManifest
from jailbee.issue_outbox import (
    OutboxSnapshot,
    PreparedBatch,
    PreparedManifest,
    RepoTarget,
    ResolvedAction,
    ResolvedIssue,
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


def _running_ci(name: str = "acme-feat-foo", *, pending: int | None = None, state: str = "Running"):
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import ContainerInfo

    return ContainerInfo(
        name=name,
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo="acme",
        uuid=f"uuid-{name}",
        git_status=GitStatus(
            wt="clean",
            ahead_diff="clean",
            ahead_count="0",
            conflict="ok",
            pending_issue_actions=pending,
        ),
    )


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


def _comment_action():
    from jailbee.issue_manifest import CommentAction

    return CommentAction(repo=".", target=ExistingIssue(42), body="looks good")


def _prepared_batch(tmp_path: Path, *, login: str = "octocat", manifest_name: str = "001.json"):
    manifest = IssueManifest(
        name=manifest_name,
        version=1,
        actions=(_comment_action(),),
        body_files=frozenset(),
    )
    repo = RepoTarget(".", tmp_path, "acme/widgets")
    resolved = ResolvedAction(0, manifest.actions[0], repo, ResolvedIssue(42), "pending")
    prepared_manifest = PreparedManifest(
        manifest, digest=_DIGEST, journal=None, actions=(resolved,)
    )
    return PreparedBatch(
        container="acme-feat-foo",
        host_repo_root=tmp_path,
        identity=_IDENTITY,
        login=login,
        outbox=OutboxSnapshot(files={manifest_name: _manifest_text()}),
        manifests=(prepared_manifest,),
        initial_issues={},
    )


# ---- registration / help ----------------------------------------------------


def test_issue_help_lists_the_visible_subcommands():
    import typer.main

    group = typer.main.get_command(app).commands["issue"]  # type: ignore[attr-defined]  # click Group

    for name in ("apply", "resolve"):
        assert not group.commands[name].hidden
    assert group.commands["show"].hidden
    # Listing and dropping issue proposals is `outbox ls|drop`, not `issue ls|drop`.
    assert "ls" not in group.commands
    assert "drop" not in group.commands


def test_apply_help_shows_its_options():
    result = runner.invoke(app, ["issue", "apply", "--help"])

    assert result.exit_code == 0, result.output
    assert "--manifest" in result.output
    assert "--yes" in result.output
    assert "--dry-run" in result.output


def test_resolve_help_shows_its_options():
    result = runner.invoke(app, ["issue", "resolve", "--help"])

    assert result.exit_code == 0, result.output
    for opt in ("--applied", "--url", "--issue", "--retry", "--yes"):
        assert opt in result.output


# ---- container selection (apply) -------------------------------------------


def test_apply_reports_nothing_pending_with_zero_candidates(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])

    result = runner.invoke(app, ["issue", "apply"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()


def test_apply_chooses_from_the_store_including_a_stopped_container(mocker, tmp_path):
    """Proposals live in the host store: a stopped container's are publishable too,
    and choosing never runs `incus exec`."""
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-old", state="Stopped")],
    )
    prepare = mocker.patch(
        "jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path)
    )
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])

    result = runner.invoke(app, ["issue", "apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert prepare.call_args.args[2] == "acme-old"


def test_apply_skips_a_container_without_an_instance_uuid(mocker, tmp_path):
    from dataclasses import replace

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch(
        "jailbee.lifecycle.list_containers", return_value=[replace(_running_ci(), uuid=None)]
    )

    result = runner.invoke(app, ["issue", "apply"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()


def test_apply_without_a_name_auto_selects_the_single_pending_container(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    prepare = mocker.patch(
        "jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path)
    )
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])

    result = runner.invoke(app, ["issue", "apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert prepare.call_args.args[2] == "acme-feat-foo"


def test_apply_asks_which_container_when_several_may_be_pending(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-feat-a"), _running_ci(name="acme-feat-b")],
    )
    pick = mocker.patch("jailbee.tui.pick_container", return_value="acme-feat-b")
    prepare = mocker.patch(
        "jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path)
    )
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])

    result = runner.invoke(app, ["issue", "apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    pick.assert_called_once()
    assert prepare.call_args.args[2] == "acme-feat-b"


def test_apply_refuses_off_a_tty_rather_than_showing_the_picker(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-feat-a"), _running_ci(name="acme-feat-b")],
    )
    pick = mocker.patch("jailbee.tui.pick_container")

    result = runner.invoke(app, ["issue", "apply"])

    assert result.exit_code == 2
    pick.assert_not_called()
    assert "feat-a" in result.output and "feat-b" in result.output


def test_apply_prints_the_plan_and_stops_at_no(mocker, tmp_path, typed_confirmations):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    batch = _prepared_batch(tmp_path)
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=batch)
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["Host GitHub login: octocat"])
    revalidate = mocker.patch("jailbee.issue_outbox.revalidate_batch")
    apply_batch = mocker.patch("jailbee.issue_outbox.apply_batch")

    result = runner.invoke(app, ["issue", "apply", "feat-foo"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Host GitHub login: octocat" in result.output
    revalidate.assert_not_called()
    apply_batch.assert_not_called()


def test_apply_refuses_off_a_tty_without_yes(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path))
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    revalidate = mocker.patch("jailbee.issue_outbox.revalidate_batch")
    apply_batch = mocker.patch("jailbee.issue_outbox.apply_batch")

    result = runner.invoke(app, ["issue", "apply", "feat-foo"])

    assert result.exit_code == 2
    assert "-y" in result.output or "--yes" in result.output
    revalidate.assert_not_called()
    apply_batch.assert_not_called()


def test_apply_dry_run_stops_before_prompt_stale_pass_and_mutation(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path))
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    revalidate = mocker.patch("jailbee.issue_outbox.revalidate_batch")
    apply_batch = mocker.patch("jailbee.issue_outbox.apply_batch")

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "a plan line" in result.output
    revalidate.assert_not_called()
    apply_batch.assert_not_called()


def test_apply_yes_skips_only_the_prompt_never_the_stale_pass(mocker, tmp_path):
    from jailbee.issue_outbox import ApplyReport

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path))
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    revalidate = mocker.patch("jailbee.issue_outbox.revalidate_batch")
    apply_batch = mocker.patch(
        "jailbee.issue_outbox.apply_batch",
        return_value=ApplyReport(applied=(), skipped=(), cleaned=("001.json",), failure=None),
    )

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    revalidate.assert_called_once()
    apply_batch.assert_called_once()


def test_apply_executes_in_exact_order(mocker, tmp_path, typed_confirmations):
    """prepare -> plan -> confirm -> stale pass -> mutation, in that order."""
    from jailbee.issue_outbox import ApplyReport

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    calls: list[str] = []
    prepare = mocker.patch(
        "jailbee.issue_outbox.prepare_batch",
        side_effect=lambda *a, **k: (calls.append("prepare"), _prepared_batch(tmp_path))[1],
    )
    plan = mocker.patch(
        "jailbee.issue_outbox.plan_lines",
        side_effect=lambda batch: (calls.append("plan"), ["a plan line"])[1],
    )
    revalidate = mocker.patch(
        "jailbee.issue_outbox.revalidate_batch", side_effect=lambda batch: calls.append("stale")
    )
    apply_batch = mocker.patch(
        "jailbee.issue_outbox.apply_batch",
        side_effect=lambda *a, **k: (
            calls.append("mutate"),
            ApplyReport(applied=(), skipped=(), cleaned=("001.json",), failure=None),
        )[1],
    )

    result = runner.invoke(app, ["issue", "apply", "feat-foo"], input="y\n")

    assert result.exit_code == 0, result.output
    assert calls == ["prepare", "plan", "stale", "mutate"]
    prepare.assert_called_once()
    plan.assert_called_once()
    revalidate.assert_called_once()
    apply_batch.assert_called_once()


def test_apply_reports_a_gate_error_and_exits_1(mocker, tmp_path):
    from jailbee.issue_outbox import IssueGateError

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch(
        "jailbee.issue_outbox.prepare_batch",
        side_effect=IssueGateError("001.json action 0: forbidden repo path 'nope'"),
    )

    result = runner.invoke(app, ["issue", "apply", "feat-foo"])

    assert result.exit_code == 1
    assert "forbidden repo path" in result.output


def test_apply_partial_failure_prints_applied_failed_and_pending(mocker, tmp_path):
    from jailbee.issue_outbox import (
        ApplyFailure,
        ApplyReport,
        PreparedManifest,
        RepoTarget,
        ResolvedAction,
        ResolvedIssue,
    )
    from jailbee.outbox_io import JournalAction

    manifest = IssueManifest(
        name="001.json",
        version=1,
        actions=(_comment_action(), _comment_action(), _comment_action()),
        body_files=frozenset(),
    )
    repo = RepoTarget(".", tmp_path, "acme/widgets")
    actions = (
        ResolvedAction(0, manifest.actions[0], repo, ResolvedIssue(42), "pending"),
        ResolvedAction(1, manifest.actions[1], repo, ResolvedIssue(42), "pending"),
        ResolvedAction(2, manifest.actions[2], repo, ResolvedIssue(42), "pending"),
    )
    prepared = PreparedManifest(manifest, digest=_DIGEST, journal=None, actions=actions)
    batch = PreparedBatch(
        container="acme-feat-foo",
        host_repo_root=tmp_path,
        identity=_IDENTITY,
        login="octocat",
        outbox=OutboxSnapshot(files={"001.json": _manifest_text()}),
        manifests=(prepared,),
        initial_issues={},
    )
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=batch)
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    mocker.patch("jailbee.issue_outbox.revalidate_batch")
    receipt = JournalAction(index=0, state="applied", repo="acme/widgets", url="https://x/1")
    mocker.patch(
        "jailbee.issue_outbox.apply_batch",
        return_value=ApplyReport(
            applied=(("001.json", receipt),),
            skipped=(),
            cleaned=(),
            failure=ApplyFailure(
                manifest="001.json", index=1, uncertain=False, detail="GitHub mutation was rejected"
            ),
        ),
    )

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "-y"])

    assert result.exit_code == 1
    assert "applied" in result.output.lower()
    assert "https://x/1" in result.output
    assert "GitHub mutation was rejected" in result.output
    # action 2 was never attempted (the run stopped at action 1's failure)
    # and must be reported as still pending.
    assert "001.json action 2: pending" in result.output


def test_apply_uncertain_failure_points_at_resolve_command(mocker, tmp_path):
    from jailbee.issue_outbox import (
        ApplyFailure,
        ApplyReport,
        PreparedManifest,
        RepoTarget,
        ResolvedAction,
        ResolvedIssue,
    )

    manifest = IssueManifest(
        name="001.json",
        version=1,
        actions=(_comment_action(), _comment_action()),
        body_files=frozenset(),
    )
    repo = RepoTarget(".", tmp_path, "acme/widgets")
    actions = (
        ResolvedAction(0, manifest.actions[0], repo, ResolvedIssue(42), "pending"),
        ResolvedAction(1, manifest.actions[1], repo, ResolvedIssue(42), "pending"),
    )
    prepared = PreparedManifest(manifest, digest=_DIGEST, journal=None, actions=actions)
    batch = PreparedBatch(
        container="acme-feat-foo",
        host_repo_root=tmp_path,
        identity=_IDENTITY,
        login="octocat",
        outbox=OutboxSnapshot(files={"001.json": _manifest_text()}),
        manifests=(prepared,),
        initial_issues={},
    )
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=batch)
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    mocker.patch("jailbee.issue_outbox.revalidate_batch")
    mocker.patch(
        "jailbee.issue_outbox.apply_batch",
        return_value=ApplyReport(
            applied=(),
            skipped=(),
            cleaned=(),
            failure=ApplyFailure(
                manifest="001.json",
                index=1,
                uncertain=True,
                detail="GitHub mutation outcome is uncertain",
            ),
        ),
    )

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "-y"], env={"COLUMNS": "200"})

    assert result.exit_code == 1
    assert (
        "jailbee issue resolve acme-feat-foo 001.json 1 "
        "(--applied --url <url> [--issue <n>] | --retry)"
    ) in result.output


# ---- ls ---------------------------------------------------------------------


def test_show_prints_the_manifest_body(mocker, tmp_path):
    long_body = "x" * 400
    text = _manifest_text(
        actions=[{"type": "comment", "repo": ".", "issue": 42, "body": long_body}]
    )
    _setup(mocker, tmp_path, files={"001.json": text})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    _stub_journals(mocker, None)

    result = runner.invoke(app, ["issue", "show", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert long_body in result.output.replace("\n", "")


def test_show_renders_bodies_to_the_terminal_width(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    mocker.patch("jailbee.outbox.markdown_view._terminal_width", return_value=40)
    text = _manifest_text(
        actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "**bold** " + "word " * 40}]
    )
    _setup(mocker, tmp_path, files={"001.json": text})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    _stub_journals(mocker, None)

    result = runner.invoke(app, ["issue", "show", "feat-foo"])

    assert result.exit_code == 0, result.output
    body_lines = [
        line for line in result.output.splitlines() if line.strip().startswith(("bold", "word"))
    ]
    assert len(body_lines) > 1 and all(len(line) <= 40 for line in body_lines)
    assert "\x1b" not in result.output  # a raw print of the styled lines would leak escapes


def test_apply_prints_the_plan_through_the_shared_markdown_printer(mocker, tmp_path):
    from jailbee.outbox.markdown_view import AnsiLine

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path))
    mocker.patch(
        "jailbee.issue_outbox.plan_lines", return_value=[AnsiLine("\x1b[1mstyled plan\x1b[0m")]
    )

    result = runner.invoke(app, ["issue", "apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "styled plan" in result.output
    assert "\x1b" not in result.output  # a raw print of the line would leak the escape


def test_show_never_touches_github(mocker, tmp_path):
    """Patches `_run_api`, the module's sole subprocess boundary -- see `ls`'s
    equivalent test for why."""
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    _stub_journals(mocker, None)
    run_api = mocker.patch("jailbee.issue_github._run_api")

    result = runner.invoke(app, ["issue", "show", "feat-foo"])

    assert result.exit_code == 0, result.output
    run_api.assert_not_called()


def test_show_can_select_one_manifest(mocker, tmp_path):
    _setup(
        mocker,
        tmp_path,
        files={
            "001.json": _manifest_text(
                actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "first body"}]
            ),
            "002.json": _manifest_text(
                actions=[{"type": "comment", "repo": ".", "issue": 42, "body": "second body"}]
            ),
        },
    )
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    _stub_journals(mocker, None)

    result = runner.invoke(app, ["issue", "show", "feat-foo", "002.json"])

    assert result.exit_code == 0, result.output
    assert "second body" in result.output
    assert "first body" not in result.output


def test_show_rejects_an_unknown_manifest(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})

    result = runner.invoke(app, ["issue", "show", "feat-foo", "nope.json"])

    assert result.exit_code == 2
    assert "nope.json" in result.output


# ---- resolve ---------------------------------------------------------------


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


def test_show_without_name_uses_the_issue_resolver(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    _stub_journals(mocker, None)
    res = mocker.patch(
        "jailbee.cli._resolve_issue_container",
        return_value=(mocker.MagicMock(), "acme-feat-foo"),
    )

    result = runner.invoke(app, ["issue", "show"])

    assert result.exit_code == 0, result.output
    assert res.call_args.args[1] is None


def test_show_without_name_and_nothing_pending_says_so(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.cli._resolve_issue_container", return_value=(mocker.MagicMock(), None))

    result = runner.invoke(app, ["issue", "show"])

    assert result.exit_code == 0
    assert "Nothing pending" in result.output


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


# ---- foreign repositories (apply) -------------------------------------------


def _foreign_batch(tmp_path):
    from dataclasses import replace

    batch = _prepared_batch(tmp_path)
    manifest = batch.manifests[0]
    foreign = RepoTarget("github:BerriAI/litellm", tmp_path, "BerriAI/litellm", foreign=True)
    action = replace(manifest.actions[0], repo=foreign)
    return replace(batch, manifests=(replace(manifest, actions=(action,)),))


def _patch_apply(mocker, tmp_path):
    from jailbee.issue_outbox import ApplyReport

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.issue_outbox.prepare_batch", return_value=_foreign_batch(tmp_path))
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])
    revalidate = mocker.patch("jailbee.issue_outbox.revalidate_batch")
    apply_batch = mocker.patch(
        "jailbee.issue_outbox.apply_batch",
        return_value=ApplyReport(applied=(), skipped=(), cleaned=("001.json",), failure=None),
    )
    return revalidate, apply_batch


def test_apply_help_shows_foreign():
    result = runner.invoke(app, ["issue", "apply", "--help"])
    assert "--foreign" in result.output


@pytest.mark.parametrize(("answer", "applied"), [("n\n", False), ("y\n", True)])
def test_apply_asks_one_question_naming_foreign_repositories(
    mocker, tmp_path, answer, applied, typed_confirmations
):
    revalidate, apply_batch = _patch_apply(mocker, tmp_path)

    result = runner.invoke(app, ["issue", "apply", "feat-foo"], input=answer)

    assert result.exit_code == 0, result.output
    assert "Publish 1 pending action to foreign repositories BerriAI/litellm?" in result.output
    assert "Proceed?" not in result.output
    assert apply_batch.called is applied
    assert revalidate.called is applied


def test_apply_yes_without_foreign_refuses_before_the_stale_pass(mocker, tmp_path):
    revalidate, apply_batch = _patch_apply(mocker, tmp_path)

    # error_plain prints to its own stderr console, whose default width of 80
    # wraps the refusal between "without" and "--foreign". CliRunner's
    # terminal_width does not reach that console.
    from jailbee.tui import err_console

    err_console.width = 120
    result = runner.invoke(app, ["issue", "apply", "feat-foo", "-y"])

    assert result.exit_code == 2, result.output
    assert "without --foreign" in result.output
    revalidate.assert_not_called()
    apply_batch.assert_not_called()


def test_apply_yes_with_foreign_publishes_without_asking(mocker, tmp_path):
    _revalidate, apply_batch = _patch_apply(mocker, tmp_path)

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "-y", "--foreign"])

    assert result.exit_code == 0, result.output
    assert "foreign repositories" not in result.output
    apply_batch.assert_called_once()


def test_apply_dry_run_with_foreign_asks_nothing(mocker, tmp_path):
    revalidate, apply_batch = _patch_apply(mocker, tmp_path)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=True)

    result = runner.invoke(app, ["issue", "apply", "feat-foo", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "foreign repositories" not in result.output
    confirm.assert_not_called()
    revalidate.assert_not_called()
    apply_batch.assert_not_called()


# ---- deprecated aliases -----------------------------------------------------


@pytest.mark.parametrize(("sub", "new"), [("show", "outbox show")])
def test_issue_alias_is_hidden_and_hints(mocker, tmp_path, sub, new):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])

    import typer.main

    group = typer.main.get_command(app).commands["issue"]  # type: ignore[attr-defined]  # click Group
    assert group.commands[sub].hidden

    result = runner.invoke(app, ["issue", sub])

    assert f"`jailbee issue {sub}` is deprecated; use `jailbee {new}`." in result.stderr
    assert "deprecated" not in result.stdout
