"""Direct tests for `pr_submodule_flow.publish_submodule_pr`."""

from __future__ import annotations

import pytest
import typer

from tests.conftest import mock_pr_agent


def _candidate(path="lib/a", commits=2, branch="feat/foo"):
    from jailbee.submodule_pr import SubCandidate

    return SubCandidate(
        path=path,
        commits=commits,
        branch=branch,
        dirty=False,
        head_sha="aaa",
        recorded_sha="aaa",
        subject="feat: work",
    )


def _env(mocker, tmp_path, *, record=None):
    from jailbee.pr import PrCreated
    from jailbee.pr_flow import PrRecord
    from jailbee.pr_outbox import Outbox
    from jailbee.submodule_pr import SubPublishResult

    cfg = mocker.MagicMock()
    cfg.repo_root = tmp_path
    cfg.container_user.uid = 1000
    cfg.upstream_remote = "origin"
    mock_pr_agent(cfg, False)
    cfg.pr.ai_description = False
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": name, "created_at": "2026-09-30T12:00:00Z"} for name in ("sampleapp-feat-foo", "c")
    ]
    incus.config_get.return_value = None
    mocker.patch("jailbee.pr_flow.validate_outbox_source")
    mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.submodule_pr.resolve_base_branch", return_value="develop")
    mocker.patch(
        "jailbee.submodule_pr.SubmodulePrState.read",
        return_value=record or PrRecord(None, None, False, False),
    )
    mocker.patch("jailbee.submodule_pr.SubmodulePrState.record")
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    mocker.patch("jailbee.pr.assert_github_remote")
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/lib-a")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={}))
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    create = mocker.patch(
        "jailbee.pr.create_pr",
        return_value=PrCreated(
            number=7, url="https://github.com/acme/lib-a/pull/7", already_existed=False
        ),
    )
    return cfg, incus, create


def test_create_returns_created_outcome_and_offers_comments(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, create = _env(mocker, tmp_path)
    from jailbee.outbox.io import PrManagement
    from jailbee.outbox_io import ContainerIdentity

    management = PrManagement(tmp_path / "locks")
    seen = []

    def offer(number, manager):
        seen.append(manager)
        assert number == 7
        assert manager is management
        assert manager.identity == ContainerIdentity("sampleapp-feat-foo", "2026-09-30T12:00:00Z")
        return 0

    outcome = publish_submodule_pr(
        cfg,
        incus,
        "sampleapp-feat-foo",
        "feat-foo",
        _candidate(),
        SubPrOptions(),
        repo_dir="/home/dev/repo",
        confirm_plan=None,
        offer_comments=offer,
        management=management,
    )
    assert (outcome.subpath, outcome.action, outcome.number) == ("lib/a", "created", 7)
    assert outcome.url == "https://github.com/acme/lib-a/pull/7"
    create.assert_called_once()
    assert seen == [management]
    assert management.identity is None


def test_confirm_plan_none_skips_the_plan_block(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    confirm = mocker.Mock()
    publish_submodule_pr(
        cfg,
        incus,
        "c",
        "s",
        _candidate(),
        SubPrOptions(),
        repo_dir="/r",
        confirm_plan=None,
        offer_comments=lambda n, m: 0,
    )
    publish_submodule_pr(
        cfg,
        incus,
        "c",
        "s",
        _candidate(),
        SubPrOptions(),
        repo_dir="/r",
        confirm_plan=confirm,
        offer_comments=lambda n, m: 0,
    )
    confirm.assert_called_once()


def test_detached_without_name_raises_exit_2(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    with pytest.raises(typer.Exit) as exc:
        publish_submodule_pr(
            cfg,
            incus,
            "c",
            "s",
            _candidate(branch=None),
            SubPrOptions(no_ai=True),
            repo_dir="/r",
            confirm_plan=None,
            offer_comments=lambda n, m: 0,
        )
    assert exc.value.exit_code == 2


def test_outbox_failures_are_returned_not_raised(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    outcome = publish_submodule_pr(
        cfg,
        incus,
        "c",
        "s",
        _candidate(),
        SubPrOptions(),
        repo_dir="/r",
        confirm_plan=None,
        offer_comments=lambda n, m: 2,
    )
    assert outcome.outbox_failures == 2


def _orch(mocker, *, candidates, recorded=(), interactive=False, picked=None):
    cfg = mocker.MagicMock()
    cfg.default_branch = "main"
    incus = mocker.MagicMock()
    incus.config_get.return_value = "main"
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    detect = mocker.patch("jailbee.submodule_pr.detect_candidates", return_value=candidates)
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=list(recorded))
    mocker.patch("jailbee.pr_submodule_flow._manifest_subpaths", return_value=set())
    mocker.patch("jailbee.prompting.is_interactive", return_value=interactive)
    pick = mocker.patch("jailbee.tui.pick_submodules_multi", return_value=picked)
    pub = mocker.patch("jailbee.pr_submodule_flow.publish_submodule_pr")
    return cfg, incus, detect, pick, pub


def _run(cfg, incus, **kw):
    from jailbee.pr_submodule_flow import publish_submodule_prs_first

    args = dict(
        enabled=True,
        yes=False,
        no_ai=False,
        no_outbox=False,
        ready=None,
        offer_comments=lambda n, m: 0,
    )
    args.update(kw)
    return publish_submodule_prs_first(cfg, incus, "c", "s", **args)


def test_disabled_runs_nothing(mocker):
    cfg, incus, detect, pick, pub = _orch(mocker, candidates=[_candidate()])
    assert _run(cfg, incus, enabled=False) == []
    assert incus.mock_calls == []
    detect.assert_not_called()
    pick.assert_not_called()
    pub.assert_not_called()


@pytest.mark.parametrize("commits", [0, None])
def test_unrecorded_submodule_without_known_commits_is_not_candidate(mocker, commits):
    cfg, incus, _, _, pub = _orch(mocker, candidates=[_candidate(commits=commits)])
    assert _run(cfg, incus, yes=True) == []
    pub.assert_not_called()


def test_recorded_zero_commit_submodule_is_candidate(mocker):
    from jailbee.pr_submodule_flow import SubPrOutcome

    cfg, incus, _, _, pub = _orch(mocker, candidates=[_candidate(commits=0)], recorded=["lib/a"])
    pub.return_value = SubPrOutcome("lib/a", "updated", 7, "u")
    assert [o.action for o in _run(cfg, incus, yes=True)] == ["updated"]


def test_off_tty_without_yes_warns_without_publishing(mocker):
    cfg, incus, _, pick, pub = _orch(mocker, candidates=[_candidate()])
    warn = mocker.patch("jailbee.pr_submodule_flow.warn")
    assert _run(cfg, incus) == []
    pub.assert_not_called()
    pick.assert_not_called()
    message = warn.call_args.args[0]
    assert all(text in message for text in ("lib/a", "--yes", "--no-submodules"))


def test_tty_selection_keeps_candidate_order(mocker):
    from jailbee.pr_submodule_flow import SubPrOutcome

    cfg, incus, _, _, pub = _orch(
        mocker,
        candidates=[_candidate("lib/b"), _candidate("lib/a"), _candidate("lib/c")],
        interactive=True,
        picked=["lib/a", "lib/b"],
    )
    pub.side_effect = lambda *args, **kwargs: SubPrOutcome(args[4].path, "created", 8, "u")
    from jailbee.pr_submodule_flow import choose_submodule_prs

    candidates = [_candidate("lib/b"), _candidate("lib/a"), _candidate("lib/c")]
    assert [c.path for c in choose_submodule_prs(candidates, yes=False)] == ["lib/b", "lib/a"]
    _run(cfg, incus)
    assert [c.args[4].path for c in pub.call_args_list] == ["lib/a", "lib/b"]


@pytest.mark.parametrize("picked", [None, []])
def test_tty_cancel_is_distinct_from_empty_selection(mocker, picked):
    cfg, incus, _, _, pub = _orch(
        mocker, candidates=[_candidate()], interactive=True, picked=picked
    )
    if picked is None:
        with pytest.raises(typer.Abort):
            _run(cfg, incus)
    else:
        assert _run(cfg, incus) == []
    pub.assert_not_called()


def test_abort_and_exit_continue_with_next_submodule(mocker):
    import click

    from jailbee.pr_submodule_flow import SubPrOutcome

    cfg, incus, _, _, pub = _orch(
        mocker, candidates=[_candidate("lib/c"), _candidate("lib/b"), _candidate("lib/a")]
    )
    pub.side_effect = [
        click.Abort(),
        click.exceptions.Exit(1),
        SubPrOutcome("lib/c", "created", 8, "u", 2),
    ]
    warn = mocker.patch("jailbee.pr_submodule_flow.warn")
    info = mocker.patch("jailbee.pr_submodule_flow.info")
    success = mocker.patch("jailbee.pr_submodule_flow.success")
    out = _run(cfg, incus, yes=True)
    assert [(o.subpath, o.action, o.outbox_failures) for o in out] == [
        ("lib/a", "declined", 0),
        ("lib/b", "failed", 0),
        ("lib/c", "created", 2),
    ]
    assert [c.args[4].path for c in pub.call_args_list] == ["lib/a", "lib/b", "lib/c"]
    assert any("gitlink" in c.args[0] and "lib/b" in c.args[0] for c in warn.call_args_list)
    assert any("outbox" in c.args[0] and "2" in c.args[0] for c in warn.call_args_list)
    assert any("Merge the submodule PRs first" in c.args[0] for c in info.call_args_list)
    assert any("#8 u" in c.args[0] for c in success.call_args_list)


def test_options_propagate_without_reconfirmation(mocker):
    from jailbee.pr_submodule_flow import SubPrOutcome

    cfg, incus, detect, pick, pub = _orch(mocker, candidates=[_candidate()])
    incus.config_get.return_value = None
    pub.return_value = SubPrOutcome("lib/a", "updated", 7, "u")
    _run(cfg, incus, yes=True, no_ai=True, no_outbox=True, ready=True)
    opts = pub.call_args.args[5]
    assert (opts.no_ai, opts.no_outbox, opts.ready, opts.yes, opts.note_merge_order) == (
        True,
        True,
        True,
        True,
        False,
    )
    assert opts.title is None and opts.as_name is None and opts.force is False
    assert pub.call_args.kwargs["confirm_plan"] is None
    assert pub.call_args.kwargs["repo_dir"] == "/r"
    assert detect.call_args.kwargs["base_branch"] == "main"
    pick.assert_not_called()


def test_detection_failure_warns(mocker):
    from jailbee.submodule_pr import SubmodulePrError

    cfg, incus, detect, _, pub = _orch(mocker, candidates=[])
    detect.side_effect = SubmodulePrError("exec failed")
    warn = mocker.patch("jailbee.pr_submodule_flow.warn")
    assert _run(cfg, incus, yes=True) == []
    assert "exec failed" in warn.call_args.args[0]
    pub.assert_not_called()


def test_no_submodules_is_silent(mocker):
    cfg, incus, _, pick, _ = _orch(mocker, candidates=[])
    warn = mocker.patch("jailbee.pr_submodule_flow.warn")
    info = mocker.patch("jailbee.pr_submodule_flow.info")
    assert _run(cfg, incus) == []
    warn.assert_not_called()
    info.assert_not_called()
    pick.assert_not_called()


def test_pending_description_selects_zero_commit_submodule(mocker, tmp_path):
    import json

    from jailbee import pr_submodule_flow as flow
    from jailbee.pr_flow import PrScope
    from jailbee.pr_outbox import Outbox

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()
    cands = [
        _candidate("lib/a", commits=0),
        _candidate("lib/b", commits=None),
        _candidate("lib/c", commits=0),
    ]
    mocker.patch("jailbee.submodule_pr.detect_candidates", return_value=cands)
    recorded = mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=["lib/extra"])
    scopes = [
        PrScope(tmp_path, "origin", "", None),
        PrScope(tmp_path / "a", "origin", "", "lib/a"),
        PrScope(tmp_path / "b", "origin", "", "lib/b"),
        PrScope(tmp_path / "c", "origin", "", "lib/c"),
    ]
    scope_fn = mocker.patch("jailbee.pr_flow.candidate_scopes", return_value=scopes)
    mocker.patch(
        "jailbee.pr_outbox.scope_slug",
        side_effect=lambda s: {
            None: "acme/main",
            "lib/a": "acme/a",
            "lib/b": "acme/b",
            "lib/c": "acme/c",
        }[s.subpath],
    )

    def manifest(repo, actions):
        return json.dumps(dict(version=1, repo=repo, pr=None, actions=actions))

    files = {
        "bad.json": "not json",
        "missing-body.json": manifest(
            "acme/c", [{"type": "description", "body_file": "missing.md"}]
        ),
        "a.json": manifest("acme/a", [{"type": "description", "body_file": "body.md"}]),
        "body.md": "Pending description",
        "b.json": json.dumps(
            dict(
                version=1,
                repo="acme/b",
                pr=7,
                actions=[{"type": "comment", "body": "Only comment"}],
            )
        ),
        "foreign.json": manifest("other/a", [{"type": "description", "body": "Foreign"}]),
        "main.json": manifest("acme/main", [{"type": "description", "body": "Main"}]),
        "x.progress.json": "not a manifest",
    }
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files))
    assert flow.submodule_pr_candidates(
        cfg, incus, "c", "s", repo_dir="/r", base_branch="main"
    ) == [cands[0]]
    assert scope_fn.call_args.kwargs["extra_paths"] == ["lib/extra"]
    assert recorded.called


@pytest.mark.parametrize("unreadable", [False, True])
def test_manifest_probe_best_effort_on_empty_or_unreadable_outbox(mocker, unreadable):
    from jailbee import pr_submodule_flow as flow
    from jailbee.pr_outbox import Outbox, OutboxReadError

    read = mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox({}))
    if unreadable:
        read.side_effect = OutboxReadError("cannot read")
    scopes = mocker.patch("jailbee.pr_flow.candidate_scopes")
    assert flow._manifest_subpaths(mocker.MagicMock(), mocker.MagicMock(), "c") == set()
    scopes.assert_not_called()


@pytest.mark.parametrize("answer", [None, [], ["lib/b", "lib/a"]])
def test_multi_picker_checked_labels_and_answer(mocker, answer):
    from jailbee import tui
    from jailbee.submodule_pr import describe_candidate

    candidates = [_candidate("lib/a"), _candidate("lib/b")]
    checkbox = mocker.patch("jailbee.tui.checkbox", return_value=answer)
    assert tui.pick_submodules_multi(candidates) == answer
    assert checkbox.call_args.args[0] == "Publish these submodule PRs first?"
    choices = checkbox.call_args.kwargs["choices"]
    assert [c.value for c in choices] == ["lib/a", "lib/b"]
    assert all(c.checked for c in choices)
    assert [c.title for c in choices] == [describe_candidate(c, width=5) for c in candidates]


@pytest.mark.parametrize("consumed", [False, True])
def test_candidate_description_must_be_pending_in_real_evidence(mocker, tmp_path, consumed):
    import json

    from jailbee import pr_submodule_flow as flow
    from jailbee.outbox_io import ContainerIdentity
    from jailbee.pr_flow import PrScope
    from jailbee.pr_outbox import Outbox

    cfg = mocker.MagicMock()
    incus = mocker.MagicMock()
    candidate = _candidate(commits=0)
    mocker.patch("jailbee.submodule_pr.detect_candidates", return_value=[candidate])
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=[])
    mocker.patch(
        "jailbee.pr_flow.candidate_scopes", return_value=[PrScope(tmp_path, "origin", "", "lib/a")]
    )
    mocker.patch("jailbee.pr_outbox.scope_slug", return_value="acme/a")
    files = {
        "a.json": json.dumps(
            dict(
                version=1,
                repo="acme/a",
                pr=7,
                actions=[
                    {"type": "description", "body": "Description"},
                    {"type": "comment", "body": "Pending comment"},
                ],
            )
        )
    }
    if consumed:
        files["a.json.progress.json"] = json.dumps(
            {"applied": [0], "urls": {"0": "https://github.com/acme/a/pull/7"}}
        )
    mocker.patch(
        "jailbee.pr_outbox.read_outbox",
        return_value=Outbox(files, identity=ContainerIdentity("c", "born")),
    )
    assert flow.submodule_pr_candidates(
        cfg, incus, "c", "s", repo_dir="/r", base_branch="main"
    ) == ([] if consumed else [candidate])


def test_acceptance_preview_uses_recorded_number_not_path_presence(mocker):
    import json

    cfg, incus, _, pick, pub = _orch(
        mocker, candidates=[_candidate("lib/a"), _candidate("lib/b")], interactive=True, picked=[]
    )
    incus.config_get.side_effect = lambda full, key: (
        json.dumps(
            {
                "lib/a": {"pr": 7, "branch": "feat/a", "author": True},
                "lib/b": {"branch": "feat/b", "author": True},
            }
        )
        if key == "user.jailbee.sub_pr"
        else "main"
    )
    info = mocker.patch("jailbee.pr_submodule_flow.info")
    assert _run(cfg, incus) == []
    assert any(
        "lib/a" in c.args[0] and "update" in c.args[0] and "#7" in c.args[0]
        for c in info.call_args_list
    )
    assert any("lib/b" in c.args[0] and "create" in c.args[0] for c in info.call_args_list)
    pick.assert_called_once()
    pub.assert_not_called()


def test_confirmation_payload_and_abort_precede_transport(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, create = _env(mocker, tmp_path)
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    transport = mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
    confirm = mocker.Mock(side_effect=typer.Abort())
    with pytest.raises(typer.Abort):
        publish_submodule_pr(
            cfg,
            incus,
            "c",
            "s",
            _candidate(),
            SubPrOptions(pr_number=9),
            repo_dir="/r",
            confirm_plan=confirm,
            offer_comments=lambda n, m: 0,
        )
    plan = confirm.call_args.args[0]
    assert (
        plan.container_full,
        plan.container_short,
        plan.subpath,
        plan.source_branch,
        plan.commits,
    ) == ("c", "s", "lib/a", "feat/foo", 2)
    assert (plan.action, plan.remote, plan.base, plan.draft, plan.notes) == (
        "update",
        "origin",
        "develop",
        None,
        (),
    )
    transport.assert_not_called()
    create.assert_not_called()


@pytest.mark.parametrize("kind", ["git", "incus", "submodule", "submodule_pr", "bug"])
def test_orchestration_isolates_known_errors_but_not_programming_errors(mocker, kind):
    from jailbee.git import GitError
    from jailbee.incus import IncusError
    from jailbee.pr_submodule_flow import SubPrOutcome
    from jailbee.submodule_pr import SubmodulePrError
    from jailbee.submodules import SubmoduleError

    errors = {
        "git": GitError,
        "incus": IncusError,
        "submodule": SubmoduleError,
        "submodule_pr": SubmodulePrError,
        "bug": ValueError,
    }
    cfg, incus, _, _, pub = _orch(mocker, candidates=[_candidate("lib/a"), _candidate("lib/b")])
    pub.side_effect = [
        errors[kind]("operational failure"),
        SubPrOutcome("lib/b", "created", 8, "u"),
    ]
    if kind == "bug":
        with pytest.raises(ValueError, match="operational failure"):
            _run(cfg, incus, yes=True)
    else:
        assert [(o.subpath, o.action) for o in _run(cfg, incus, yes=True)] == [
            ("lib/a", "failed"),
            ("lib/b", "created"),
        ]
