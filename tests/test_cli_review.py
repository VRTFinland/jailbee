"""CLI tests for the `jailbee review` group.

Fully mocked: `pr_outbox`'s container reads, every `gh` call (including
`pr.gh_login`, which the plan's identity line would otherwise really run)
and the TTY check are patched, so nothing here touches Incus, GitHub or a
terminal.

These tests assert on the commands' *observable* behaviour — exit code,
what is printed, whether `apply_manifest`/`finalize` were called — never on
the shape of the loop inside `review_apply_cmd`, so Task 12's extraction of
that loop into `pr_outbox` leaves them passing unchanged.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _manifest_text(**overrides) -> str:
    """One manifest's JSON text. Copied from `tests/test_pr_outbox.py`."""
    payload = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 1234,
        "head_sha": "abc1234",
        "actions": [{"type": "comment", "body": "looks good"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _a_target(name: str = "001-x.json", *, text: str | None = None, stale: bool = False):
    """A resolved `Target` for `_manifest_text()`, as `resolve_target` returns."""
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import Target, parse_manifest

    manifest = parse_manifest(name, text if text is not None else _manifest_text(), {})
    info = PrInfo(
        number=1234,
        head_ref="feat-foo",
        head_sha="abc1234",
        state="OPEN",
        base_ref="main",
    )
    return Target(manifest=manifest, pr=info, stale=stale)


def _null_pr_target(name: str = "001-x.json"):
    """A `Target` for a `pr: null` manifest — description only, no PR yet."""
    from jailbee.pr_outbox import Target, parse_manifest

    text = _manifest_text(pr=None, actions=[{"type": "description", "body": "new body"}])
    return Target(manifest=parse_manifest(name, text, {}), pr=None, stale=False)


def _running_ci(name: str = "acme-feat-foo", *, pending: int | None = 1, state: str = "Running"):
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
            pending_pr_actions=pending,
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
    # The plan's identity line calls `gh` for real otherwise, and the
    # confirmation is impossible off a TTY — both patched so every test below
    # exercises the interactive path deliberately.
    mocker.patch("jailbee.pr.gh_login", return_value="octocat")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    from jailbee.pr_outbox import Outbox

    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files=files or {}))
    return cfg, incus


# ---- apply ----------------------------------------------------------------


def test_apply_reports_nothing_pending(mocker, tmp_path):
    _setup(mocker, tmp_path)

    result = runner.invoke(app, ["review", "apply", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()


def test_apply_prints_the_plan_and_stops_at_no(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "looks good" in result.output  # the plan was shown
    apply_mock.assert_not_called()  # and nothing was published


def test_apply_publishes_on_yes(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome

    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
    )
    finalize = mocker.patch("jailbee.pr_outbox.finalize")

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="y\n")

    assert result.exit_code == 0, result.output
    apply_mock.assert_called_once()
    finalize.assert_called_once()
    assert "https://x/c" in result.output


def test_apply_exits_1_when_publishing_fails(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome

    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(), urls=(), failure="HTTP 500"),
    )

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="y\n")

    assert result.exit_code == 1
    assert "HTTP 500" in result.output
    # And the user is told what state that leaves them in.
    assert "still pending" in result.output


def test_apply_refuses_a_stopped_container(mocker, tmp_path):
    from jailbee.pr_outbox import OutboxReadError

    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        side_effect=OutboxReadError("could not read the outbox in acme-feat-foo: not running"),
    )

    result = runner.invoke(app, ["review", "apply", "feat-foo"])

    assert result.exit_code == 2
    assert "jailbee start" in result.output


def test_dry_run_never_prompts_and_never_publishes(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo", "--dry-run"])

    assert result.exit_code == 0, result.output
    apply_mock.assert_not_called()


def test_apply_names_the_publishing_identity(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 action" in result.output
    assert "octocat" in result.output


def test_apply_omits_the_identity_clause_when_gh_login_is_unknown(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr.gh_login", return_value=None)
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())

    result = runner.invoke(app, ["review", "apply", "feat-foo", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 action" in result.output
    assert " as " not in result.output


def test_apply_refuses_off_a_tty_without_yes(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo"])

    assert result.exit_code == 2
    assert "-y" in result.output
    apply_mock.assert_not_called()


def test_apply_publishes_without_a_prompt_off_a_tty_with_yes(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome

    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
    )
    mocker.patch("jailbee.pr_outbox.finalize")

    result = runner.invoke(app, ["review", "apply", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    apply_mock.assert_called_once()


def test_apply_routes_a_pr_null_manifest_to_jb_pr(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_null_pr_target())
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "does not exist yet" in result.output
    assert "jailbee pr feat-foo" in result.output
    apply_mock.assert_not_called()


def test_apply_reports_every_failure_before_the_plan(mocker, tmp_path):
    from jailbee.pr_outbox import GateError

    _setup(
        mocker,
        tmp_path,
        files={"001-x.json": _manifest_text(), "002-y.json": _manifest_text()},
    )
    mocker.patch(
        "jailbee.pr_outbox.resolve_target",
        side_effect=[GateError("manifest 001-x.json targets acme/other"), _a_target("002-y.json")],
    )
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="n\n")

    # The refusal is reported, and reported *before* any part of the plan.
    assert "acme/other" in result.output
    assert result.output.index("acme/other") < result.output.index("looks good")
    # Declining after a reported failure still exits non-zero: something the
    # user asked for could not be done.
    assert result.exit_code == 1
    apply_mock.assert_not_called()


def test_apply_reports_a_finalize_failure_and_exits_1(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome, FinalizeError

    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
    )
    mocker.patch(
        "jailbee.pr_outbox.finalize",
        side_effect=FinalizeError("001-x.json: applied [0] but the sidecar could not be written"),
    )

    result = runner.invoke(app, ["review", "apply", "feat-foo"], input="y\n")

    assert result.exit_code == 1
    # What landed is still reported, next to why it could not be recorded.
    assert "https://x/c" in result.output
    assert "sidecar could not be written" in result.output


def test_apply_stops_before_the_next_manifest_after_a_failed_publish(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome

    _setup(
        mocker,
        tmp_path,
        files={"001-x.json": _manifest_text(), "002-y.json": _manifest_text()},
    )
    mocker.patch(
        "jailbee.pr_outbox.resolve_target",
        side_effect=[_a_target("001-x.json"), _a_target("002-y.json")],
    )
    apply_mock = mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(), urls=(), failure="HTTP 500"),
    )
    mocker.patch("jailbee.pr_outbox.finalize")

    result = runner.invoke(app, ["review", "apply", "feat-foo", "-y"])

    assert result.exit_code == 1
    assert apply_mock.call_count == 1, "the second manifest must not be attempted"
    assert "002-y.json" in result.output, "and the user must be told it is still pending"


def test_apply_stops_before_the_next_manifest_when_it_cannot_record(mocker, tmp_path):
    """A container that can no longer be written to must not receive more posts."""
    from jailbee.pr_outbox import ApplyOutcome, FinalizeError

    _setup(
        mocker,
        tmp_path,
        files={"001-x.json": _manifest_text(), "002-y.json": _manifest_text()},
    )
    mocker.patch(
        "jailbee.pr_outbox.resolve_target",
        side_effect=[_a_target("001-x.json"), _a_target("002-y.json")],
    )
    apply_mock = mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
    )
    mocker.patch("jailbee.pr_outbox.finalize", side_effect=FinalizeError("sidecar write failed"))

    result = runner.invoke(app, ["review", "apply", "feat-foo", "-y"])

    assert result.exit_code == 1
    assert apply_mock.call_count == 1
    assert "002-y.json" in result.output


def test_apply_without_a_name_uses_the_single_pending_container(mocker, tmp_path):
    from jailbee.pr_outbox import ApplyOutcome

    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    apply_mock = mocker.patch(
        "jailbee.pr_outbox.apply_manifest",
        return_value=ApplyOutcome(applied=(0,), urls=("https://x/c",), failure=None),
    )
    mocker.patch("jailbee.pr_outbox.finalize")

    result = runner.invoke(app, ["review", "apply", "-y"])

    assert result.exit_code == 0, result.output
    apply_mock.assert_called_once()


def test_apply_without_a_name_and_nothing_pending_exits_0(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci(pending=0)])

    result = runner.invoke(app, ["review", "apply"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()


# ---- ls -------------------------------------------------------------------


def test_ls_lists_pending_manifests_as_json(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target())
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])

    result = runner.invoke(app, ["review", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert "001-x.json" in result.output


def test_ls_marks_a_pr_null_manifest_as_for_jb_pr(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_null_pr_target())
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])

    result = runner.invoke(app, ["review", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows[0]["state"] == "for jb pr"


def test_ls_reports_staleness_instead_of_failing_on_it(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    resolve = mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target(stale=True))
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])

    result = runner.invoke(app, ["review", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["state"] == "stale"
    # `ls` reports, never refuses: it asks for the forcing resolution so a
    # moved head is a column value rather than a GateError.
    assert resolve.call_args.kwargs["force"] is True


def test_ls_notes_containers_it_could_not_read(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_running_ci(name="acme-feat-off", state="Stopped")],
    )

    result = runner.invoke(app, ["review", "ls"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower()


def test_ls_counts_actions_by_type(mocker, tmp_path):
    text = _manifest_text(
        actions=[
            {"type": "review", "body": "summary", "comments": []},
            {"type": "reply", "comment_id": 7, "body": "ack"},
            {"type": "reply", "comment_id": 8, "body": "ack"},
        ]
    )
    _setup(mocker, tmp_path, files={"001-x.json": text})
    mocker.patch("jailbee.pr_outbox.resolve_target", return_value=_a_target(text=text))
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[_running_ci()])

    result = runner.invoke(app, ["review", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["actions"] == "review:1 reply:2"


# ---- show -----------------------------------------------------------------


def test_show_prints_bodies_whole(mocker, tmp_path):
    long_body = "x" * 400
    _setup(
        mocker,
        tmp_path,
        files={"001-x.json": _manifest_text(actions=[{"type": "comment", "body": long_body}])},
    )

    result = runner.invoke(app, ["review", "show", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert long_body in result.output.replace("\n", "")


def test_show_can_select_one_manifest(mocker, tmp_path):
    _setup(
        mocker,
        tmp_path,
        files={
            "001-x.json": _manifest_text(actions=[{"type": "comment", "body": "first body"}]),
            "002-y.json": _manifest_text(actions=[{"type": "comment", "body": "second body"}]),
        },
    )

    result = runner.invoke(app, ["review", "show", "feat-foo", "002-y.json"])

    assert result.exit_code == 0, result.output
    assert "second body" in result.output
    assert "first body" not in result.output


def test_show_rejects_an_unknown_manifest(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})

    result = runner.invoke(app, ["review", "show", "feat-foo", "nope.json"])

    assert result.exit_code == 2
    assert "nope.json" in result.output


def test_show_reports_a_malformed_manifest_without_crashing(mocker, tmp_path):
    _setup(mocker, tmp_path, files={"001-x.json": "{not json"})

    result = runner.invoke(app, ["review", "show", "feat-foo"])

    assert result.exit_code == 1
    assert "001-x.json" in result.output


# ---- drop -----------------------------------------------------------------


def test_drop_deletes_without_publishing(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})
    apply_mock = mocker.patch("jailbee.pr_outbox.apply_manifest")

    result = runner.invoke(app, ["review", "drop", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    apply_mock.assert_not_called()
    assert any("rm" in c.args[1] for c in incus.exec.call_args_list)


def test_drop_asks_first_and_keeps_the_manifest_on_no(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})

    result = runner.invoke(app, ["review", "drop", "feat-foo"], input="n\n")

    assert result.exit_code == 0, result.output
    incus.exec.assert_not_called()


def test_drop_takes_one_named_manifest(mocker, tmp_path):
    _, incus = _setup(
        mocker,
        tmp_path,
        files={"001-x.json": _manifest_text(), "002-y.json": _manifest_text()},
    )

    result = runner.invoke(app, ["review", "drop", "feat-foo", "002-y.json", "-y"])

    assert result.exit_code == 0, result.output
    deleted = [arg for call in incus.exec.call_args_list for arg in call.args[1]]
    assert any(a.endswith("002-y.json") for a in deleted)
    assert not any(a.endswith("001-x.json") for a in deleted)


def test_drop_rejects_an_unknown_manifest(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path, files={"001-x.json": _manifest_text()})

    result = runner.invoke(app, ["review", "drop", "feat-foo", "nope.json", "-y"])

    assert result.exit_code == 2
    # Named, and next to what *is* pending — an exit code alone would also be
    # what a missing `review` command produced.
    assert "nope.json" in result.output
    assert "001-x.json" in result.output
    incus.exec.assert_not_called()


def test_drop_reports_nothing_to_drop(mocker, tmp_path):
    _, incus = _setup(mocker, tmp_path)

    result = runner.invoke(app, ["review", "drop", "feat-foo", "-y"])

    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output.lower()
    incus.exec.assert_not_called()
