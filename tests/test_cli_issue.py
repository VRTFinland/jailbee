"""CLI tests for the `jailbee issue` group.

Fully mocked: `issue_outbox`'s container reads and every orchestration call
(`prepare_batch`, `plan_lines`, `revalidate_batch`, `apply_batch`,
`reconcile_action`, `drop_manifest`) are patched, so nothing here touches
Incus or GitHub. The TTY check is patched the same way `test_cli_review.py`
patches it.
"""

from __future__ import annotations

import json
from pathlib import Path

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

runner = CliRunner()

_IDENTITY = ContainerIdentity(full_name="acme-feat-foo", created_at="2026-01-01T00:00:00Z")
_DIGEST = "a" * 64


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
        git_status=GitStatus(
            wt="clean",
            ahead_diff="clean",
            ahead_count="0",
            conflict="ok",
            pending_issue_actions=pending,
        ),
    )


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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.issue_outbox.read_issue_outbox",
        return_value=OutboxSnapshot(files=files or {}),
    )
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


def test_issue_help_lists_all_five_subcommands():
    result = runner.invoke(app, ["issue", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("ls", "show", "apply", "drop", "resolve"):
        assert name in result.output


def test_ls_help_shows_format_and_fields_options():
    result = runner.invoke(app, ["issue", "ls", "--help"])

    assert result.exit_code == 0, result.output
    assert "--format" in result.output
    assert "-o" in result.output
    assert "--fields" in result.output


def test_apply_help_shows_its_options():
    result = runner.invoke(app, ["issue", "apply", "--help"])

    assert result.exit_code == 0, result.output
    assert "--manifest" in result.output
    assert "--yes" in result.output
    assert "--dry-run" in result.output


def test_drop_help_shows_its_options():
    result = runner.invoke(app, ["issue", "drop", "--help"])

    assert result.exit_code == 0, result.output
    assert "--archive-journal" in result.output
    assert "--yes" in result.output


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


def test_apply_ignores_a_stopped_container_when_choosing(mocker, tmp_path):
    # A stopped container's outbox has a pending manifest too -- if the
    # `Running` guard in `_resolve_issue_container` were removed, it would
    # become the single auto-selected candidate instead of being excluded.
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-old", state="Stopped")],
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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-feat-a"), _running_ci(name="acme-feat-b")],
    )
    pick = mocker.patch("jailbee.tui.pick_container")

    result = runner.invoke(app, ["issue", "apply"])

    assert result.exit_code == 2
    pick.assert_not_called()
    assert "feat-a" in result.output and "feat-b" in result.output


def test_apply_probe_zero_container_is_never_read(mocker, tmp_path):
    """The probe's explicit 0 means "nothing here" -- the outbox is not even
    opened, unlike an unknown (`None`) count."""
    _setup(mocker, tmp_path)
    read = mocker.patch(
        "jailbee.issue_outbox.read_issue_outbox",
        return_value=OutboxSnapshot(files={"001.json": _manifest_text()}),
    )
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci(pending=0)])

    result = runner.invoke(app, ["issue", "apply"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()
    read.assert_not_called()


def test_apply_probe_none_container_is_still_read(mocker, tmp_path):
    """`None` means the probe could not say -- the outbox stays authoritative."""
    _setup(mocker, tmp_path)
    read = mocker.patch(
        "jailbee.issue_outbox.read_issue_outbox",
        return_value=OutboxSnapshot(files={"001.json": _manifest_text()}),
    )
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci(pending=None)])
    prepare = mocker.patch(
        "jailbee.issue_outbox.prepare_batch", return_value=_prepared_batch(tmp_path)
    )
    mocker.patch("jailbee.issue_outbox.plan_lines", return_value=["a plan line"])

    result = runner.invoke(app, ["issue", "apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    # Once to decide candidacy, once more in `_read_issue_outbox_or_exit` --
    # unrelated to this pre-filter, and true before it too.
    assert read.call_count == 2
    assert prepare.call_args.args[2] == "acme-feat-foo"


# ---- approval and ordering (apply) -----------------------------------------


def test_apply_prints_the_plan_and_stops_at_no(mocker, tmp_path):
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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
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


def test_apply_executes_in_exact_order(mocker, tmp_path):
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


def test_ls_renders_one_row_per_manifest_as_json(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)

    result = runner.invoke(app, ["issue", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["manifest"] == "001.json"
    assert rows[0]["state"] == "pending"


def test_ls_never_touches_github(mocker, tmp_path):
    """`ls` must render from the outbox/journal alone -- no `gh` adapter call.

    Patches `_run_api`, the module's sole subprocess boundary, so this holds
    for every present and future adapter function, not just `get_issue`.
    """
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    run_api = mocker.patch("jailbee.issue_github._run_api")

    result = runner.invoke(app, ["issue", "ls"])

    assert result.exit_code == 0, result.output
    run_api.assert_not_called()


def test_ls_skips_reading_a_container_whose_probe_says_zero(mocker, tmp_path):
    _setup(mocker, tmp_path)
    read = mocker.patch(
        "jailbee.issue_outbox.read_issue_outbox",
        return_value=OutboxSnapshot(files={"001.json": _manifest_text()}),
    )
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci(pending=0)])

    result = runner.invoke(app, ["issue", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    read.assert_not_called()


def test_ls_with_a_name_does_not_probe_git_status(mocker, tmp_path):
    """A single named container has nothing to pre-filter between -- the
    outbox read is the authoritative answer either way, so probing every
    running container of the repo for its git status just to keep one row
    buys nothing."""
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    list_containers = mocker.patch(
        "jailbee.lifecycle.list_containers", return_value=[_running_ci()]
    )
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)

    result = runner.invoke(app, ["issue", "ls", "acme-feat-foo", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert list_containers.call_args.kwargs.get("with_git_status") is not True


def test_ls_notes_a_stopped_container_instead_of_reading_it(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-feat-off", state="Stopped")],
    )

    result = runner.invoke(app, ["issue", "ls"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower() or "not checked" in result.output.lower()


def test_ls_marks_a_manifest_with_an_uncertain_receipt(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    mocker.patch(
        "jailbee.outbox_io.container_identity",
        return_value=_IDENTITY,
    )
    journal = IssueJournal(
        identity=_IDENTITY,
        manifest_name="001.json",
        digest=_DIGEST,
        action_count=1,
        actions=(
            JournalAction(
                index=0, state="uncertain", repo="acme/widgets", detail="uncertain outcome"
            ),
        ),
    )
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=journal)

    result = runner.invoke(app, ["issue", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows[0]["state"] == "uncertain"


# ---- show ---------------------------------------------------------------------


def test_show_prints_the_manifest_body(mocker, tmp_path):
    long_body = "x" * 400
    text = _manifest_text(
        actions=[{"type": "comment", "repo": ".", "issue": 42, "body": long_body}]
    )
    _setup(mocker, tmp_path, files={"001.json": text})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=None)

    result = runner.invoke(app, ["issue", "show", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert long_body in result.output.replace("\n", "")


def test_show_never_touches_github(mocker, tmp_path):
    """Patches `_run_api`, the module's sole subprocess boundary -- see `ls`'s
    equivalent test for why."""
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=None)
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
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=None)

    result = runner.invoke(app, ["issue", "show", "feat-foo", "002.json"])

    assert result.exit_code == 0, result.output
    assert "second body" in result.output
    assert "first body" not in result.output


def test_show_rejects_an_unknown_manifest(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})

    result = runner.invoke(app, ["issue", "show", "feat-foo", "nope.json"])

    assert result.exit_code == 2
    assert "nope.json" in result.output


# ---- drop ---------------------------------------------------------------------


def test_drop_removes_never_started_files_by_default(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    drop = mocker.patch("jailbee.issue_outbox.drop_manifest", return_value=("001.json",))

    result = runner.invoke(app, ["issue", "drop", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    drop.assert_called_once()
    assert drop.call_args.kwargs["archive_journal"] is False


def test_drop_never_touches_github(mocker, tmp_path):
    """Patches `_run_api`, the module's sole subprocess boundary -- see `ls`'s
    equivalent test for why."""
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    mocker.patch("jailbee.issue_outbox.drop_manifest", return_value=("001.json",))
    run_api = mocker.patch("jailbee.issue_github._run_api")

    result = runner.invoke(app, ["issue", "drop", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    run_api.assert_not_called()


def test_drop_refuses_recorded_progress_without_archive_journal(mocker, tmp_path):
    from jailbee.outbox_io import JournalError

    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    mocker.patch(
        "jailbee.issue_outbox.drop_manifest",
        side_effect=JournalError("001.json: cannot drop a manifest with recorded progress"),
    )

    result = runner.invoke(app, ["issue", "drop", "feat-foo", "-y"])

    assert result.exit_code == 1
    assert "recorded progress" in result.output


def test_drop_archive_journal_shows_applied_receipts_and_pending_then_archives(mocker, tmp_path):
    manifest_text = _manifest_text(
        actions=[
            {"type": "comment", "repo": ".", "issue": 42, "body": "first"},
            {"type": "comment", "repo": ".", "issue": 42, "body": "second"},
        ]
    )
    _setup(mocker, tmp_path, files={"001.json": manifest_text})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    journal = IssueJournal(
        identity=_IDENTITY,
        manifest_name="001.json",
        digest=_DIGEST,
        action_count=2,
        actions=(JournalAction(index=0, state="applied", repo="acme/widgets", url="https://x/1"),),
    )
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=journal)
    drop = mocker.patch("jailbee.issue_outbox.drop_manifest", return_value=("001.json",))

    result = runner.invoke(app, ["issue", "drop", "feat-foo", "--archive-journal", "-y"])

    assert result.exit_code == 0, result.output
    assert "https://x/1" in result.output
    assert "action 1" in result.output and "pending" in result.output.lower()
    drop.assert_called_once()
    assert drop.call_args.kwargs["archive_journal"] is True


def test_drop_archive_journal_still_refuses_uncertainty(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    journal = IssueJournal(
        identity=_IDENTITY,
        manifest_name="001.json",
        digest=_DIGEST,
        action_count=1,
        actions=(JournalAction(index=0, state="uncertain", repo="acme/widgets", detail="x"),),
    )
    mocker.patch("jailbee.outbox_io.JournalStore.load", return_value=journal)
    drop = mocker.patch("jailbee.issue_outbox.drop_manifest")

    result = runner.invoke(app, ["issue", "drop", "feat-foo", "--archive-journal", "-y"])

    assert result.exit_code == 1
    assert "uncertain" in result.output.lower()
    drop.assert_not_called()


def test_drop_asks_first_and_keeps_the_manifest_on_no(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001.json": _manifest_text()})
    mocker.patch("jailbee.outbox_io.container_identity", return_value=_IDENTITY)
    drop = mocker.patch("jailbee.issue_outbox.drop_manifest")

    result = runner.invoke(app, ["issue", "drop", "feat-foo"], input="n\n")

    assert result.exit_code == 0, result.output
    drop.assert_not_called()


# ---- resolve --------------------------------------------------------------


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
    mocker.patch(
        "jailbee.outbox_io.JournalStore.load", return_value=journal if journal_actions else None
    )
    mocker.patch(
        "jailbee.issue_outbox.resolve_repo_targets",
        return_value={".": RepoTarget(".", tmp_path, "acme/widgets")},
    )


def test_resolve_requires_exactly_one_of_applied_or_retry(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path)

    result = runner.invoke(app, ["issue", "resolve", "feat-foo", "001.json", "0"])

    assert result.exit_code == 2
    assert "--applied" in result.output or "--retry" in result.output


def test_resolve_rejects_both_applied_and_retry(mocker, tmp_path):
    _setup_resolve(mocker, tmp_path)
    reconcile = mocker.patch("jailbee.issue_outbox.reconcile_action")

    result = runner.invoke(
        app,
        ["issue", "resolve", "feat-foo", "001.json", "0", "--applied", "--url", "x", "--retry"],
    )

    assert result.exit_code == 2
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


def test_resolve_create_action_requires_issue(mocker, tmp_path):
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
    assert "--issue" in result.output


def test_resolve_renders_the_action_before_confirming(mocker, tmp_path):
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
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
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
