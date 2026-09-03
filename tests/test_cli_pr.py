"""CLI tests for `gie pr` (and the hidden `gie git pr` alias)."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app


def _publish_result(
    dirty: bool = False,
    publish_name: str = "feat/foo",
    forced: bool = False,
    branch: str = "feat/foo",
):
    from jailbee.sync import FetchResult, PublishResult

    return PublishResult(
        fetch=FetchResult(
            branch=branch,
            old_oid="abc1234",
            new_oid="def5678",
            base_oid="abc1234",
            commits_added=2,
        ),
        dirty=dirty,
        publish_name=publish_name,
        forced=forced,
    )


def _pr_created(already: bool = False):
    from jailbee.pr import PrCreated

    return PrCreated(
        number=123, url="https://github.com/acme/widgets/pull/123", already_existed=already
    )


def _setup(mocker, tmp_path, labels=None):
    """Wire cfg/incus/short-name mocks; `labels` feeds incus.config_get."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    incus_mock = mocker.MagicMock()
    label_map = (
        labels
        if labels is not None
        else {"user.jailbee.base_branch": "main", "user.jailbee.branch": "feat/foo"}
    )
    incus_mock.config_get.side_effect = lambda name, key: label_map.get(key)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus_mock, "sampleapp-feat-foo"),
    )
    # Preflight passes by default; tests that need the mount/stopped/no-clone
    # failure re-patch this with a SyncError side_effect.
    mocker.patch(
        "jailbee.sync.assert_container_publishable",
        return_value="sampleapp-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    # The create path asks GitHub whether the container's branch already has a
    # PR. Stubbed to "no" for every test; the tests that care re-patch it.
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    cfg_mock.claude.enabled = False
    cfg_mock.claude.ai_pr_description = True
    cfg_mock.upstream_remote = "origin"
    return cfg_mock, incus_mock


def _publish_via_hook(mocker, published):
    """Patch `publish_branch_from_container` so it runs the CLI's pre-push hook.

    The real function calls `on_before_push` between the container fetch and
    the push, and that hook is where `jailbee pr` prints its fetch summary and
    dirty-tree warning — a stub that only returns a `PublishResult` skips all
    of that output. Returns the patched mock.
    """
    mocker.patch("jailbee.git.log_oneline", return_value=["def5678 feat: do thing"])

    def fake_publish(*_args, on_before_push=None, **_kwargs):
        assert on_before_push is not None, "`jailbee pr` must pass the pre-push hook"
        on_before_push(published)
        return published

    return mocker.patch("jailbee.sync.publish_branch_from_container", side_effect=fake_publish)


def test_create_pr_happy_path(mocker, tmp_path):
    _, incus_mock = _setup(mocker, tmp_path)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    publish.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["head"] == "feat/foo"
    assert kwargs["base"] == "main"  # from user.jailbee.base_branch label
    assert kwargs["title"] == "feat: do thing"  # last commit subject
    assert kwargs["draft"] is True
    assert "feat-foo" in kwargs["body"]  # placeholder mentions the container
    assert "https://github.com/acme/widgets/pull/123" in result.output
    incus_mock.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr", "123")
    incus_mock.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_author", "1")


def test_pr_reports_the_fetch_and_announces_the_push_before_pushing(mocker, tmp_path):
    """The pre-push hook prints the fetch summary, dirty warning and push line.

    `git push` inherits its output and prints nothing until the remote answers.
    Reporting the fetch only *after* the publish returns would leave git's own
    fetch output as the last thing on screen, so a push blocked on remote
    authentication reads as a hung fetch.
    """
    _setup(mocker, tmp_path)
    _publish_via_hook(mocker, _publish_result())
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    summary = "refs/jailbee/feat-foo/feat/foo: abc1234..def5678 (2 new commits)"
    announce = "Pushing 'feat/foo' to origin"
    assert summary in result.output
    assert announce in result.output
    # The summary reaches the terminal first: the push is what may block.
    assert result.output.index(summary) < result.output.index(announce)


def test_create_pr_title_falls_back_to_branch_name(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value=None)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert create.call_args.kwargs["title"] == "feat/foo"


def test_create_pr_explicit_flags_override_defaults(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    subject = mocker.patch("jailbee.git.commit_subject")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(
        app,
        [
            "pr",
            "feat-foo",
            "--title",
            "My title",
            "--body",
            "My body",
            "--base",
            "develop",
            "--no-draft",
        ],
    )

    assert result.exit_code == 0, result.output
    kwargs = create.call_args.kwargs
    assert kwargs["title"] == "My title"
    assert kwargs["body"] == "My body"
    assert kwargs["base"] == "develop"
    assert kwargs["draft"] is False
    subject.assert_not_called()  # explicit --title skips the subject lookup


def test_pr_rerun_author_container_takes_update_path(mocker, tmp_path):
    _update_setup(mocker, tmp_path)

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "updated" in result.output.lower()


def test_create_pr_missing_base_label_requires_flag(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={})  # no user.jailbee.base_branch
    publish = mocker.patch("jailbee.sync.publish_branch_from_container")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--base" in result.output
    publish.assert_not_called()


def test_create_pr_dirty_tree_warns(mocker, tmp_path):
    _setup(mocker, tmp_path)
    _publish_via_hook(mocker, _publish_result(dirty=True))
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "uncommitted" in result.output.lower()


def test_create_pr_sync_error_exits_1(mocker, tmp_path):
    from jailbee.sync import SyncError

    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=SyncError("Container 'feat-foo' is not running. ..."),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "not running" in result.output


def test_create_pr_pr_error_exits_1(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    from jailbee.pr import PrCreateError

    mocker.patch(
        "jailbee.pr.create_pr",
        side_effect=PrCreateError("'gh' is not authenticated. Run: gh auth login"),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "authenticated" in result.output


def test_create_pr_web_opens_browser(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    web = mocker.patch("jailbee.pr.open_pr_in_browser")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--web"])

    assert result.exit_code == 0, result.output
    web.assert_called_once()
    assert web.call_args.args[1] == 123


def test_create_pr_label_order_pr_author_first(mocker, tmp_path):
    """pr_author must be written BEFORE user.jailbee.pr so a partial write is harmless.

    If only pr_author lands, the entry guard (which keys on user.jailbee.pr) sees
    nothing and allows a re-run.  If only user.jailbee.pr lands, the guard mistakes
    the container for a `gie new --pr` container and permanently blocks re-runs.
    """
    _, incus_mock = _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: order")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    calls = incus_mock.config_set.call_args_list
    # Locate the two label-write calls among all config_set invocations.
    pr_author_call = next(c for c in calls if c.args[-1] == "1" and "pr_author" in c.args[-2])
    pr_number_call = next(
        c for c in calls if c.args[-1] == "123" and c.args[-2] == "user.jailbee.pr"
    )
    assert calls.index(pr_author_call) < calls.index(pr_number_call), (
        "user.jailbee.pr_author must be written before user.jailbee.pr"
    )


def test_git_pr_alias_calls_same_function(mocker, tmp_path):
    _setup(mocker, tmp_path)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["git", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    publish.assert_called_once()


def test_create_pr_label_write_failure_still_prints_url(mocker, tmp_path):
    """Finding 1: config_set raising IncusError must not suppress the PR URL."""
    from jailbee.incus import IncusError

    _, incus_mock = _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    incus_mock.config_set.side_effect = IncusError("boom")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/widgets/pull/123" in result.output
    assert "label" in result.output.lower()


def test_create_pr_branch_override_forwarded(mocker, tmp_path):
    """Finding 3: --branch is forwarded to publish_branch_from_container."""
    _setup(mocker, tmp_path)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--branch", "feat/x"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["branch"] == "feat/x"


def test_create_pr_fresh_success_includes_branch(mocker, tmp_path):
    """Finding 4: fresh-create success line includes the branch name."""
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "created for 'feat/foo'" in result.output
    assert "https://github.com/acme/widgets/pull/123" in result.output


def _enable_ai(cfg_mock):
    cfg_mock.claude.enabled = True
    cfg_mock.claude.ai_pr_description = True


def test_create_pr_uses_ai_text_when_enabled(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="AI body", branch="feat/foo"),
    )
    subject = mocker.patch("jailbee.git.commit_subject")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    assert gen.call_args.kwargs["base"] == "main"
    assert gen.call_args.kwargs["branch"] == "feat/foo"
    kwargs = create.call_args.kwargs
    assert kwargs["title"] == "AI title"
    assert kwargs["body"] == "AI body"
    subject.assert_not_called()  # AI supplied the title; no commit-subject lookup


def test_create_pr_falls_back_when_ai_returns_none(mocker, tmp_path):
    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=None)
    mocker.patch("jailbee.git.commit_subject", return_value="feat: subj")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "failed" in result.output.lower()  # warning shown
    kwargs = create.call_args.kwargs
    assert kwargs["title"] == "feat: subj"  # commit-subject fallback
    assert "feat-foo" in kwargs["body"]  # placeholder fallback


def test_create_pr_no_ai_flag_skips_generation(mocker, tmp_path):
    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    mocker.patch("jailbee.git.commit_subject", return_value="feat: subj")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--no-ai"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()


def test_create_pr_ai_disabled_by_config(mocker, tmp_path):
    cfg_mock, _ = _setup(mocker, tmp_path)
    cfg_mock.claude.enabled = True
    cfg_mock.claude.ai_pr_description = False
    cfg_mock.claude.ai_pr_branch = False  # both AI surfaces off == "AI disabled"
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    mocker.patch("jailbee.git.commit_subject", return_value="feat: subj")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()


def test_create_pr_both_explicit_skips_generation(mocker, tmp_path):
    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    cfg_mock.claude.ai_pr_branch = False  # branch AI off: both fields explicit -> no AI at all
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(
        app,
        ["pr", "feat-foo", "--title", "T", "--body", "B"],
    )

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    kwargs = create.call_args.kwargs
    assert kwargs["title"] == "T"
    assert kwargs["body"] == "B"


def test_create_pr_only_title_explicit_generates_body(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="T", body="AI body", branch="feat/foo"),
    )
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--title", "T"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    assert gen.call_args.kwargs["fixed_title"] == "T"
    assert gen.call_args.kwargs["fixed_body"] is None
    assert create.call_args.kwargs["body"] == "AI body"


def test_create_pr_only_body_explicit_generates_title(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg_mock, _ = _setup(mocker, tmp_path)
    _enable_ai(cfg_mock)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="B", branch="feat/foo"),
    )
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--body", "B"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    assert gen.call_args.kwargs["fixed_body"] == "B"
    assert gen.call_args.kwargs["fixed_title"] is None
    assert create.call_args.kwargs["title"] == "AI title"
    assert create.call_args.kwargs["body"] == "B"


def test_pr_create_ready_makes_nondraft(mocker, tmp_path):
    """--ready flag sets draft=False and outputs 'PR' (not 'Draft PR')."""
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: ready test")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--ready"])

    assert result.exit_code == 0, result.output
    assert create.call_args.kwargs["draft"] is False
    assert "PR #123 created" in result.output
    assert "Draft PR" not in result.output


def test_pr_create_draft_flag_makes_draft(mocker, tmp_path):
    """--draft flag explicitly sets draft=True and outputs 'Draft PR'."""
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: draft test")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--draft"])

    assert result.exit_code == 0, result.output
    assert create.call_args.kwargs["draft"] is True
    assert "Draft PR #123" in result.output


def _update_setup(mocker, tmp_path):
    """Author container (pr + pr_author labels) with an existing PR #123."""
    cfg, incus = _setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.pr": "123",
            "user.jailbee.pr_author": "1",
            "user.jailbee.base_branch": "main",
        },
    )
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    return cfg, incus


def test_pr_update_pushes_without_recreating(mocker, tmp_path):
    _update_setup(mocker, tmp_path)
    create = mocker.patch("jailbee.pr.create_pr")
    edit = mocker.patch("jailbee.pr.edit_pr")
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])  # non-TTY → no prompt

    assert result.exit_code == 0, result.output
    create.assert_not_called()
    edit.assert_not_called()
    gen.assert_not_called()  # no AI waste on a plain update
    assert "updated" in result.output.lower()


def test_pr_update_explicit_title_edits(mocker, tmp_path):
    _update_setup(mocker, tmp_path)
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--title", "New title"])

    assert result.exit_code == 0, result.output
    edit.assert_called_once()
    assert edit.call_args.kwargs["title"] == "New title"
    assert edit.call_args.kwargs["body"] is None
    assert "title updated" in result.output.lower()
    assert "description refreshed" not in result.output.lower()


def test_pr_update_description_regenerates(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg, _ = _update_setup(mocker, tmp_path)
    _enable_ai(cfg)
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI T", body="AI B", branch="feat/foo"),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--description"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    edit.assert_called_once_with(tmp_path, 123, title="AI T", body="AI B")
    assert "refreshed" in result.output.lower()


def test_pr_update_prompt_yes_regenerates(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg, _ = _update_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=True)
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="T", body="B", branch="feat/foo"),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    edit.assert_called_once()


def test_pr_update_prompt_no_skips(mocker, tmp_path):
    cfg, _ = _update_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    edit.assert_not_called()


def test_pr_update_no_ai_never_prompts(mocker, tmp_path):
    cfg, _ = _update_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm")
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--no-ai"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    edit.assert_not_called()


def test_pr_update_description_flag_without_ai_warns_and_skips(mocker, tmp_path):
    _update_setup(mocker, tmp_path)
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--description", "--no-ai"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    edit.assert_not_called()
    assert "cannot regenerate the description without claude" in result.output.lower()


def test_pr_update_ready_toggles_state(mocker, tmp_path):
    _update_setup(mocker, tmp_path)
    ready = mocker.patch("jailbee.pr.set_ready")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--ready"])

    assert result.exit_code == 0, result.output
    ready.assert_called_once_with(tmp_path, 123, True)
    assert "marked ready" in result.output.lower()


def test_pr_update_draft_toggles_state(mocker, tmp_path):
    _update_setup(mocker, tmp_path)
    ready = mocker.patch("jailbee.pr.set_ready")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--draft"])

    assert result.exit_code == 0, result.output
    ready.assert_called_once_with(tmp_path, 123, False)


def test_pr_update_via_already_exists_fallback(mocker, tmp_path):
    """No pr_author label, but the PR already exists → still treated as update."""
    _setup(mocker, tmp_path)  # base_branch=main, no pr labels
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="t")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created(already=True))

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "updated" in result.output.lower()


# --- Task 6: publish-name decision, --as, --force, local-branch reconcile -----


def test_as_flag_overrides_name_and_skips_ai(mocker, tmp_path):
    cfg, incus = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    cfg.claude.ai_pr_description = False  # branch AI on, desc AI off -> --as skips all AI
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/x"),
    )
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    mocker.patch("jailbee.git.commit_subject", return_value="feat: x")
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "user/x"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "user/x"
    assert create.call_args.kwargs["head"] == "user/x"
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_branch", "user/x")


def test_ai_branch_used_non_tty(mocker, tmp_path):
    from jailbee.pr_ai import PrText

    cfg, incus = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    cfg.claude.ai_pr_description = True
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="feat: nice", body="B", branch="user/nice"),
    )
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/nice"),
    )
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.cli.sys.stdin.isatty", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["publish_name"] == "user/nice"
    assert create.call_args.kwargs["head"] == "user/nice"
    # The AI-proposed head name (not just the --as path) is persisted as a label.
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_branch", "user/nice")


def test_reconcile_renames_local_branch(mocker, tmp_path):
    # container branch is "dev-1", PR head becomes "feat/foo"; the local
    # container-branch ref exists, the PR-head ref does not -> rename fires.
    cfg, _ = _setup(
        mocker,
        tmp_path,
        labels={"user.jailbee.base_branch": "main", "user.jailbee.branch": "dev-1"},
    )
    cfg.claude.enabled = False  # no AI; publish name defaults to container branch
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="feat/foo", branch="dev-1"),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: x")
    le = mocker.patch("jailbee.git.local_branch_exists")
    le.side_effect = lambda root, b: b == "dev-1"
    rename = mocker.patch("jailbee.git.rename_branch")
    mocker.patch("jailbee.git.set_upstream")
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    rename.assert_called_once_with(cfg.repo_root, "dev-1", "feat/foo")


def test_force_requires_explicit_name(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        side_effect=AssertionError("must not resolve/pick before the --force guard"),
    )
    result = CliRunner().invoke(app, ["pr", "--force"])
    assert result.exit_code == 2
    assert "explicit container name" in result.output


def test_force_threads_into_publish(mocker, tmp_path):
    _setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.base_branch": "main",
            "user.jailbee.pr_branch": "user/nice",
            "user.jailbee.pr": "123",
            "user.jailbee.pr_author": "1",
        },
    )
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/nice", forced=True),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["force"] is True
    assert publish.call_args.kwargs["publish_name"] == "user/nice"  # update path reuses label
    assert "head force-pushed (--force-with-lease)" in result.output


def test_update_path_reuses_stored_label_skips_ai(mocker, tmp_path):
    cfg, _ = _setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.base_branch": "main",
            "user.jailbee.pr_branch": "user/nice",
            "user.jailbee.pr": "123",
            "user.jailbee.pr_author": "1",
        },
    )
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/nice"),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()  # branch AI skipped on update
    assert publish.call_args.kwargs["publish_name"] == "user/nice"


# --- Task 6 follow-up: ai_pr_branch / ai_pr_description are independent -------


def test_branch_ai_only_desc_off(mocker, tmp_path):
    """Branch AI on, description AI off: AI names the head; title/body do NOT."""
    from jailbee.pr_ai import PrText

    cfg, _ = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    cfg.claude.ai_pr_description = False
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="AI body", branch="user/ai"),
    )
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/ai"),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: from commit")
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.cli.sys.stdin.isatty", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()  # single call yields the branch name
    assert publish.call_args.kwargs["publish_name"] == "user/ai"
    kwargs = create.call_args.kwargs
    assert kwargs["head"] == "user/ai"
    assert kwargs["title"] == "feat: from commit"  # commit subject, NOT the AI title
    assert kwargs["title"] != "AI title"
    assert "feat-foo" in kwargs["body"]  # placeholder, NOT the AI body
    assert kwargs["body"] != "AI body"


def test_desc_ai_only_branch_off(mocker, tmp_path):
    """Description AI on, branch AI off: AI writes title/body; head stays container branch."""
    from jailbee.pr_ai import PrText

    cfg, _ = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = False
    cfg.claude.ai_pr_description = True
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="AI body", branch="user/ai"),
    )
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="feat/foo"),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: from commit")
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    assert publish.call_args.kwargs["publish_name"] == "feat/foo"  # container branch, NOT user/ai
    kwargs = create.call_args.kwargs
    assert kwargs["head"] == "feat/foo"
    assert kwargs["title"] == "AI title"
    assert kwargs["body"] == "AI body"


def test_as_with_desc_ai(mocker, tmp_path):
    """--as fixes the head, but description AI still runs for the title/body."""
    from jailbee.pr_ai import PrText

    cfg, _ = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_description = True
    gen = mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="AI body", branch="user/ai"),
    )
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/manual"),
    )
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "user/manual"])

    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    assert publish.call_args.kwargs["publish_name"] == "user/manual"
    kwargs = create.call_args.kwargs
    assert kwargs["head"] == "user/manual"
    assert kwargs["title"] == "AI title"


# --- Task 6 whole-branch review: fail-fast preflight + safe label ordering ----


def test_pr_mount_mode_fails_before_ai(mocker, tmp_path):
    """A mount-mode/stopped/no-clone container must fail BEFORE AI runs."""
    from jailbee.sync import SyncError

    cfg, _ = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    mocker.patch(
        "jailbee.sync.assert_container_publishable",
        side_effect=SyncError("container 'feat-foo' is in mount mode — ..."),
    )
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    publish = mocker.patch("jailbee.sync.publish_branch_from_container")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "mount mode" in result.output
    gen.assert_not_called()  # AI must not run before the preflight
    publish.assert_not_called()


def test_create_stores_labels_in_safe_order(mocker, tmp_path):
    """On create, labels are written pr_branch -> pr_author -> pr (pr last)."""
    from jailbee.pr_ai import PrText

    cfg, incus = _setup(mocker, tmp_path)
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    cfg.claude.ai_pr_description = True
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI title", body="AI body", branch="user/ai"),
    )
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="user/ai"),
    )
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.cli.sys.stdin.isatty", return_value=False)
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    pr_labels = {"user.jailbee.pr_branch", "user.jailbee.pr_author", "user.jailbee.pr"}
    written = [c.args[-2] for c in incus.config_set.call_args_list if c.args[-2] in pr_labels]
    assert written == ["user.jailbee.pr_branch", "user.jailbee.pr_author", "user.jailbee.pr"]


def test_label_write_failure_is_nonfatal(mocker, tmp_path):
    """If the FINAL user.jailbee.pr write fails, the PR still succeeds and pr_branch
    + pr_author landed first (so a re-run resolves the correct head)."""
    from jailbee.incus import IncusError

    _, incus = _setup(mocker, tmp_path)  # claude disabled -> non-AI create path
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: subj")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    def _cfg_set(name, key, value):
        if key == "user.jailbee.pr":
            raise IncusError("boom")

    incus.config_set.side_effect = _cfg_set

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/widgets/pull/123" in result.output
    assert "label" in result.output.lower()  # best-effort warning printed
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_branch", "feat/foo")
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_author", "1")


def test_pr_open_opens_browser_and_skips_publish(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "123"})
    publish = mocker.patch("jailbee.sync.publish_branch_from_container")
    create = mocker.patch("jailbee.pr.create_pr")
    open_web = mocker.patch("jailbee.pr.open_pr_in_browser")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--open"])

    assert result.exit_code == 0, result.output
    open_web.assert_called_once_with(tmp_path, 123)
    publish.assert_not_called()
    create.assert_not_called()


def test_pr_open_works_for_review_checkout_container(mocker, tmp_path):
    # A `gie new --pr` container has user.jailbee.pr but no user.jailbee.pr_author;
    # the normal path rejects it, but --open short-circuits before that guard.
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "456"})
    open_web = mocker.patch("jailbee.pr.open_pr_in_browser")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--open"])

    assert result.exit_code == 0, result.output
    open_web.assert_called_once_with(tmp_path, 456)


def test_pr_open_errors_when_no_pr_label(mocker, tmp_path):
    _setup(mocker, tmp_path, labels={})
    open_web = mocker.patch("jailbee.pr.open_pr_in_browser")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--open"])

    assert result.exit_code == 1
    assert "no associated PR" in result.output
    open_web.assert_not_called()


# --- Adopting a `gie new --pr` container's PR head -------------------------


def _review_pr_info(
    number: int = 456,
    head_ref: str = "contributor/fix-worktime",
    state: str = "OPEN",
    cross: bool = False,
    owner: str | None = "acme",
    author: str | None = "someone",
):
    from jailbee.pr import PrInfo

    return PrInfo(
        number=number,
        head_ref=head_ref,
        head_sha="a" * 40,
        state=state,
        base_ref="master",
        author_login=author,
        is_cross_repository=cross,
        head_repo_owner=owner,
    )


def _review_setup(mocker, tmp_path, extra_labels=None):
    """A `gie new --pr 456` container: user.jailbee.pr, no pr_author/pr_adopted."""
    labels = {
        "user.jailbee.pr": "456",
        "user.jailbee.branch": "alice/worktime-stomp",
        "user.jailbee.base_branch": "master",
    }
    if extra_labels:
        labels.update(extra_labels)
    cfg, incus = _setup(mocker, tmp_path, labels=labels)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(
            publish_name="alice/worktime-stomp", branch="alice/worktime-stomp"
        ),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    return cfg, incus, publish


def test_pr_adopts_review_container_after_confirmation(mocker, tmp_path):
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="adopt")
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"
    create.assert_not_called()


def test_pr_adopt_writes_pr_branch_before_pr_adopted(mocker, tmp_path):
    """A partial label write must never leave a container adopted without a
    head name — a re-run would then publish to the container branch."""
    _cfg, incus, _publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="adopt")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    writes = [tuple(c.args[-2:]) for c in incus.config_set.call_args_list]
    assert writes.index(("user.jailbee.pr_branch", "contributor/fix-worktime")) < writes.index(
        ("user.jailbee.pr_adopted", "1")
    )


def test_pr_adopt_declined_does_nothing(mocker, tmp_path):
    """Cancelling the publish menu must leave the container untouched."""
    _cfg, incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value=None)

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code != 0
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_adopt_without_tty_requires_yes(mocker, tmp_path):
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    pick = mocker.patch("jailbee.pr_flow._pick_review_action")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    pick.assert_not_called()
    publish.assert_not_called()


def test_pr_adopt_yes_skips_the_prompt(mocker, tmp_path):
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    pick = mocker.patch("jailbee.pr_flow._pick_review_action")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    pick.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"


def test_pr_adopt_refuses_fork_pr(mocker, tmp_path):
    _cfg, incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=_review_pr_info(cross=True, owner="contributor"),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 1
    assert "contributor" in result.output
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_adopt_refuses_as_flag(mocker, tmp_path):
    """--as is refused before the PR is adopted — a usage error must not
    record the adoption as a side effect."""
    _cfg, incus, publish = _review_setup(mocker, tmp_path)
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "other/branch", "--yes"])

    assert result.exit_code == 2
    assert "--as" in result.output
    publish.assert_not_called()
    resolve.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_adopted_container_still_refuses_as_flag(mocker, tmp_path):
    """Regression: --as used to be checked only on the FIRST run, so later runs
    on an adopted container ignored it and pushed to the PR head anyway."""
    _cfg, _incus, publish = _review_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.pr_adopted": "1",
            "user.jailbee.pr_branch": "contributor/fix-worktime",
        },
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "other/branch"])

    assert result.exit_code == 2
    assert "PR #456" in result.output
    publish.assert_not_called()


def test_pr_authored_container_refuses_as_flag(mocker, tmp_path):
    """Same on a gie-authored PR: --as used to be a silent no-op there."""
    _cfg, _incus = _update_setup(mocker, tmp_path)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=AssertionError("must not publish after a --as usage error"),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "other/branch"])

    assert result.exit_code == 2
    assert "PR #123" in result.output
    publish.assert_not_called()


def test_pr_adopt_warns_on_closed_pr_but_proceeds(mocker, tmp_path):
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info(state="MERGED"))

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    assert "MERGED" in result.output
    publish.assert_called_once()


def test_pr_already_adopted_container_skips_gh_and_prompt(mocker, tmp_path):
    _cfg, _incus, publish = _review_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.pr_adopted": "1",
            "user.jailbee.pr_branch": "alice/worktime-stomp",
        },
    )
    resolve = mocker.patch("jailbee.pr.resolve_pr")
    pick = mocker.patch("jailbee.pr_flow._pick_review_action")
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    resolve.assert_not_called()
    pick.assert_not_called()
    create.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "alice/worktime-stomp"


def test_pr_adopted_push_failure_points_at_pr_refresh(mocker, tmp_path):
    from jailbee.sync import SyncError

    _cfg, _incus, _publish = _review_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.pr_adopted": "1",
            "user.jailbee.pr_branch": "alice/worktime-stomp",
        },
    )
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=SyncError("Pushing 'alice/worktime-stomp' to origin failed: non-ff"),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--pr --rebase" in result.output
    assert "write access" in result.output


def test_pr_adopt_yes_states_the_pr_before_pushing(mocker, tmp_path):
    """A scripted --yes run must say which PR it is about to mutate."""
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    assert "PR #456 by @someone" in result.output
    assert "contributor/fix-worktime" in result.output
    assert "master" in result.output
    publish.assert_called_once()


# --- --force on a PR head gie did not create -------------------------------


def _adopted_setup(mocker, tmp_path):
    """An already-adopted `gie new --pr 456` container."""
    return _review_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.pr_adopted": "1",
            "user.jailbee.pr_branch": "contributor/fix-worktime",
        },
    )


def test_pr_force_on_foreign_head_asks_for_confirmation(mocker, tmp_path):
    _cfg, _incus, publish = _adopted_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code == 0, result.output
    confirm.assert_called_once()
    assert "contributor/fix-worktime" in confirm.call_args.args[0]
    assert publish.call_args.kwargs["force"] is True


def test_pr_force_on_foreign_head_declined_does_not_push(mocker, tmp_path):
    _cfg, _incus, publish = _adopted_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code != 0
    publish.assert_not_called()


def test_pr_force_on_foreign_head_without_tty_requires_yes(mocker, tmp_path):
    _cfg, _incus, publish = _adopted_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert "PR #456" in result.output
    publish.assert_not_called()


def test_pr_force_on_foreign_head_yes_skips_confirmation(mocker, tmp_path):
    _cfg, _incus, publish = _adopted_setup(mocker, tmp_path)
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force", "--yes"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert publish.call_args.kwargs["force"] is True


def test_pr_force_on_authored_pr_is_not_gated(mocker, tmp_path):
    """gie created this PR — --force keeps working without an extra prompt."""
    _update_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm")
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(forced=True),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert publish.call_args.kwargs["force"] is True


# --- the description of a foreign PR is never rewritten unasked -------------


def test_pr_foreign_head_never_offers_description_regen(mocker, tmp_path):
    """Regression: every run on an adopted container used to offer to replace
    the PR author's description."""
    cfg, _incus, _publish = _adopted_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm")
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    gen.assert_not_called()
    edit.assert_not_called()
    assert "description unchanged" in result.output


def test_pr_authored_pr_still_offers_description_regen(mocker, tmp_path):
    """The other direction: gie's own PR keeps the interactive offer."""
    from jailbee.pr_ai import PrText

    cfg, _incus = _update_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="T", body="B", branch="feat/foo"),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "description" in confirm.call_args.args[0].lower()
    edit.assert_called_once()


def test_pr_foreign_head_explicit_description_still_applies(mocker, tmp_path):
    """Suppressing the offer must not disable an explicit --description."""
    from jailbee.pr_ai import PrText

    cfg, _incus, _publish = _adopted_setup(mocker, tmp_path)
    _enable_ai(cfg)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="AI T", body="AI B", branch="contributor/fix-worktime"),
    )
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--description"])

    assert result.exit_code == 0, result.output
    edit.assert_called_once_with(tmp_path, 123, title="AI T", body="AI B")


def test_pr_foreign_head_explicit_title_still_applies(mocker, tmp_path):
    _cfg, _incus, _publish = _adopted_setup(mocker, tmp_path)
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--title", "New title"])

    assert result.exit_code == 0, result.output
    assert edit.call_args.kwargs["title"] == "New title"


def test_pr_authored_push_failure_has_no_pr_refresh_hint(mocker, tmp_path):
    from jailbee.sync import SyncError

    _update_setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=SyncError("Pushing 'feat/foo' to origin failed: non-ff"),
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--pr --rebase" not in result.output


# ---------------------------------------------------------------------------
# A branch that already has a PR, on a container gie knows nothing about
# (`gie new <existing-branch>`, no --pr) — see pr.find_pr_for_branch
# ---------------------------------------------------------------------------


def _existing_pr_info(state="OPEN", cross=False, author="someone", head_ref="feat/foo"):
    from jailbee.pr import PrInfo

    return PrInfo(
        number=77,
        head_ref=head_ref,
        head_sha="c" * 40,
        state=state,
        base_ref="main",
        author_login=author,
        is_cross_repository=cross,
        head_repo_owner="acme",
    )


def _branch_pr_setup(mocker, tmp_path, pr_info):
    """Label-less container whose branch already has `pr_info` on GitHub."""
    cfg, incus = _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=pr_info)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    return cfg, incus, publish


def test_pr_updates_the_existing_pr_for_the_branch(mocker, tmp_path):
    """Regression: gie opened a SECOND PR for a branch that already had one.

    Nothing on the container says "PR" (it came from `gie new <branch>`), so the
    create path ran, Claude proposed a fresh head branch name, and `gh pr
    create` happily opened a duplicate.
    """
    _cfg, incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=True)
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    create.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "feat/foo"
    assert "77" in result.output
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr", "77")
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_branch", "feat/foo")
    incus.config_set.assert_any_call("sampleapp-feat-foo", "user.jailbee.pr_adopted", "1")


def test_pr_existing_pr_is_not_claimed_as_gie_authored(mocker, tmp_path):
    """`user.jailbee.pr_author` marks a PR gie opened; this one it merely found.

    Writing it would switch off the foreign-head guards: `--force` would stop
    asking before overwriting the head, and the description could be
    regenerated over the author's text.
    """
    _cfg, incus, _publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    written = [c.args[-2] for c in incus.config_set.call_args_list]
    assert "user.jailbee.pr_author" not in written


def test_pr_existing_pr_writes_pr_branch_before_pr_adopted(mocker, tmp_path):
    _cfg, incus, _publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    writes = [tuple(c.args[-2:]) for c in incus.config_set.call_args_list]
    assert writes.index(("user.jailbee.pr_branch", "feat/foo")) < writes.index(
        ("user.jailbee.pr_adopted", "1")
    )


def test_pr_existing_pr_declined_publishes_nothing(mocker, tmp_path):
    _cfg, incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code != 0
    publish.assert_not_called()
    create.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_existing_pr_without_tty_requires_yes(mocker, tmp_path):
    _cfg, _incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    confirm.assert_not_called()
    publish.assert_not_called()


def test_pr_existing_pr_yes_skips_the_prompt(mocker, tmp_path):
    _cfg, _incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "feat/foo"


def test_pr_existing_pr_suppresses_ai_branch_naming(mocker, tmp_path):
    """An adopted head is fixed, so there is nothing for Claude to name."""
    cfg, _incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    cfg.claude.enabled = True
    cfg.claude.ai_pr_branch = True
    gen = mocker.patch("jailbee.pr_ai.generate_pr_text")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    gen.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "feat/foo"


def test_pr_closed_pr_for_branch_opens_a_new_one(mocker, tmp_path):
    """A merged/closed PR is not a target for further work — open a fresh one."""
    _cfg, _incus, _publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info(state="MERGED"))
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    create.assert_called_once()
    confirm.assert_not_called()
    assert "77" in result.output  # the closed PR is still reported
    assert "MERGED" in result.output


def test_pr_fork_pr_for_branch_opens_a_new_one(mocker, tmp_path):
    """A fork PR's head lives in the fork; our branch is a different thing."""
    _cfg, _incus, _publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info(cross=True))
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    create.assert_called_once()
    confirm.assert_not_called()


def test_pr_as_flag_skips_the_existing_pr_lookup(mocker, tmp_path):
    """`--as` is a deliberate request for a separate PR under another head."""
    _setup(mocker, tmp_path)
    find = mocker.patch("jailbee.pr.find_pr_for_branch")
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name="other/x"),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--as", "other/x"])

    assert result.exit_code == 0, result.output
    find.assert_not_called()
    assert create.call_args.kwargs["head"] == "other/x"


def test_pr_lookup_is_skipped_on_a_container_that_already_has_a_pr(mocker, tmp_path):
    """An authored container takes the update path without asking GitHub."""
    _update_setup(mocker, tmp_path)
    find = mocker.patch("jailbee.pr.find_pr_for_branch")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    find.assert_not_called()


def test_pr_force_on_a_found_pr_asks_for_confirmation(mocker, tmp_path):
    """The head belongs to a PR gie did not open, so --force must confirm.

    This is the consequence of not recording `user.jailbee.pr_author` for a PR that
    was merely found: the foreign-head guard stays on.
    """
    _cfg, _incus, publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--force"])

    assert result.exit_code == 0, result.output
    # Two confirmations: adopt the PR, then overwrite its head.
    assert confirm.call_count == 2
    assert "77" in confirm.call_args.args[0]
    assert publish.call_args.kwargs["force"] is True


def test_pr_found_pr_description_is_not_offered_for_regeneration(mocker, tmp_path):
    """The author's text is never replaced without an explicit --description."""
    cfg, _incus, _publish = _branch_pr_setup(mocker, tmp_path, _existing_pr_info())
    cfg.claude.enabled = True
    cfg.claude.ai_pr_description = True
    # A TTY is what would make the offer appear at all — patch it so the
    # suppression, not the absence of a terminal, is what this test proves.
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)
    edit = mocker.patch("jailbee.pr.edit_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    edit.assert_not_called()
    confirm.assert_not_called()


# --- Binding a container to an existing PR by number (`--pr N`) -------------


def _bind_setup(mocker, tmp_path, labels=None):
    """A plain `jailbee new <branch>` container whose branch name has nothing
    to do with the PR head — the case `--pr N` exists for."""
    label_map = {
        "user.jailbee.branch": "local-scratch",
        "user.jailbee.base_branch": "master",
    }
    if labels:
        label_map.update(labels)
    cfg, incus = _setup(mocker, tmp_path, labels=label_map)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(
            publish_name="contributor/fix-worktime", branch="local-scratch"
        ),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    return cfg, incus, publish


def test_pr_binds_to_a_numbered_pr_whose_head_differs_from_the_branch(mocker, tmp_path):
    """The whole point: `gh pr view <branch>` finds nothing, so only the
    number can name the PR."""
    _cfg, _incus, publish = _bind_setup(mocker, tmp_path)
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    find = mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 0, result.output
    assert resolve.call_args.args[1] == 456
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"
    create.assert_not_called()
    # The number-based bind replaces the name lookup rather than racing it.
    find.assert_not_called()


def test_pr_bind_records_the_pr_as_adopted_not_authored(mocker, tmp_path):
    """jailbee did not open this PR, so the foreign-head guards must stay on."""
    _cfg, incus, _publish = _bind_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 0, result.output
    writes = {tuple(c.args[-2:]) for c in incus.config_set.call_args_list}
    assert ("user.jailbee.pr_branch", "contributor/fix-worktime") in writes
    assert ("user.jailbee.pr", "456") in writes
    assert ("user.jailbee.pr_adopted", "1") in writes
    assert not any(key == "user.jailbee.pr_author" for key, _ in writes)


def test_pr_bind_writes_pr_branch_before_the_pr_number(mocker, tmp_path):
    """A partial write must not leave a number without a head name — the
    re-run would then publish to the container branch."""
    _cfg, incus, _publish = _bind_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 0, result.output
    writes = [tuple(c.args[-2:]) for c in incus.config_set.call_args_list]
    assert writes.index(("user.jailbee.pr_branch", "contributor/fix-worktime")) < writes.index(
        ("user.jailbee.pr", "456")
    )


def test_pr_bind_asks_before_pushing_to_someone_elses_pr(mocker, tmp_path):
    _cfg, incus, publish = _bind_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456"])

    assert result.exit_code != 0
    confirm.assert_called_once()
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_without_tty_requires_yes(mocker, tmp_path):
    _cfg, incus, publish = _bind_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_refuses_a_merged_pr(mocker, tmp_path):
    """Name-based adoption silently opens a new PR for a closed one. An
    explicit number must not: the user named this PR on purpose."""
    _cfg, incus, publish = _bind_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info(state="MERGED"))
    create = mocker.patch("jailbee.pr.create_pr")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 1
    assert "MERGED" in result.output
    publish.assert_not_called()
    create.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_refuses_a_fork_pr(mocker, tmp_path):
    _cfg, incus, publish = _bind_setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.pr.resolve_pr", return_value=_review_pr_info(cross=True, owner="contributor")
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 1
    assert "contributor" in result.output
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_reports_an_unresolvable_number(mocker, tmp_path):
    from jailbee.pr import PrResolveError

    _cfg, _incus, publish = _bind_setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.pr.resolve_pr", side_effect=PrResolveError("PR #999 not found in this repo.")
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "999", "--yes"])

    assert result.exit_code == 1
    assert "999" in result.output
    publish.assert_not_called()


def test_pr_bind_rejects_as_flag(mocker, tmp_path):
    """--pr targets an existing PR's head; --as names the head of one still to
    be created. A usage error must not resolve or record anything."""
    _cfg, incus, publish = _bind_setup(mocker, tmp_path)
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--as", "x/y", "--yes"])

    assert result.exit_code == 2
    assert "--as" in result.output and "--pr" in result.output
    resolve.assert_not_called()
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_to_the_same_number_is_a_no_op(mocker, tmp_path):
    """Re-running with the same --pr must not re-ask or re-resolve."""
    _cfg, _incus, publish = _bind_setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.pr": "456",
            "user.jailbee.pr_branch": "contributor/fix-worktime",
            "user.jailbee.pr_adopted": "1",
        },
    )
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    confirm = mocker.patch("typer.confirm")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456"])

    assert result.exit_code == 0, result.output
    resolve.assert_not_called()
    confirm.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"


def test_pr_bind_retarget_to_another_number_needs_confirmation(mocker, tmp_path):
    """Without this the only way out of a typo'd --pr is `incus config unset`."""
    _cfg, incus, publish = _bind_setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.pr": "111",
            "user.jailbee.pr_branch": "old/head",
            "user.jailbee.pr_adopted": "1",
        },
    )
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456"])

    assert result.exit_code != 0
    assert "111" in confirm.call_args.args[0] and "456" in confirm.call_args.args[0]
    publish.assert_not_called()
    incus.config_set.assert_not_called()


def test_pr_bind_retarget_confirmed_replaces_the_recorded_head(mocker, tmp_path):
    _cfg, incus, publish = _bind_setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.pr": "111",
            "user.jailbee.pr_branch": "old/head",
            "user.jailbee.pr_adopted": "1",
        },
    )
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456", "--yes"])

    assert result.exit_code == 0, result.output
    writes = {tuple(c.args[-2:]) for c in incus.config_set.call_args_list}
    assert ("user.jailbee.pr", "456") in writes
    assert ("user.jailbee.pr_branch", "contributor/fix-worktime") in writes
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"


def test_pr_bind_same_number_as_review_container_asks_to_adopt_not_retarget(mocker, tmp_path):
    """`jailbee new --pr 456` leaves `user.jailbee.pr=456` with no
    `pr_branch` (`_review_setup`'s exact container shape). `record.number ==
    number` is then True but `record.head` is falsy, so the early
    already-bound return is skipped — the fix is that the wording chosen for
    that fall-through is the adoption question, not a retarget one (there is
    nothing to retarget from: the number is unchanged)."""
    _cfg, _incus, publish = _review_setup(mocker, tmp_path)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=True)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--pr", "456"])

    assert result.exit_code == 0, result.output
    question = confirm.call_args.args[0]
    assert "retarget" not in question
    assert question == "Push this container's commits to PR #456 ('contributor/fix-worktime')?"
    assert publish.call_args.kwargs["publish_name"] == "contributor/fix-worktime"


# --- `jailbee pr --stacked`: a PR against the reviewed PR's head -----------


def _stacked_setup(
    mocker,
    tmp_path,
    extra_labels=None,
    publish_name="fix/worktime-review",
    pr_info=None,
):
    """A `jailbee new --pr 456` container whose branch IS the PR head."""
    labels = {
        "user.jailbee.pr": "456",
        "user.jailbee.branch": "contributor/fix-worktime",
        "user.jailbee.base_branch": "master",
    }
    if extra_labels:
        labels.update(extra_labels)
    _cfg, incus = _setup(mocker, tmp_path, labels=labels)
    publish = mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        return_value=_publish_result(publish_name=publish_name, branch="contributor/fix-worktime"),
    )
    mocker.patch("jailbee.pr.resolve_pr", return_value=pr_info if pr_info else _review_pr_info())
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    mocker.patch("jailbee.git.commit_subject", return_value="fix: worktime")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    retarget = mocker.patch("jailbee.sync.retarget_container")
    return incus, publish, create, retarget


def test_stacked_opens_a_pr_based_on_the_reviewed_head(mocker, tmp_path):
    _incus, publish, create, _retarget = _stacked_setup(mocker, tmp_path)

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review", "--no-retarget"]
    )

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["publish_name"] == "fix/worktime-review"
    kwargs = create.call_args.kwargs
    assert kwargs["head"] == "fix/worktime-review"
    assert kwargs["base"] == "contributor/fix-worktime"  # the reviewed PR's head


def test_stacked_records_its_own_labels_and_leaves_the_parent_alone(mocker, tmp_path):
    incus, _publish, _create, _retarget = _stacked_setup(mocker, tmp_path)

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review", "--no-retarget"]
    )

    assert result.exit_code == 0, result.output
    writes = [tuple(c.args[-2:]) for c in incus.config_set.call_args_list]
    assert ("user.jailbee.stacked_pr", "123") in writes
    assert ("user.jailbee.stacked_pr_branch", "fix/worktime-review") in writes
    assert ("user.jailbee.stacked_pr_author", "1") in writes
    assert ("user.jailbee.stacked_pr_base", "contributor/fix-worktime") in writes
    # The reviewed PR stays the container's `pr` label: `jailbee ls` and
    # `jailbee git push --pr` go on meaning #456.
    assert not [w for w in writes if w[0] in {"user.jailbee.pr", "user.jailbee.pr_adopted"}]


def test_stacked_refuses_to_publish_under_the_reviewed_head(mocker, tmp_path):
    """Without --as the proposed head defaults to the container branch, which
    IS the reviewed PR's head — publishing there would silently update that PR
    instead of opening a stacked one."""
    _incus, publish, create, _retarget = _stacked_setup(
        mocker, tmp_path, publish_name="contributor/fix-worktime"
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--no-ai"])

    assert result.exit_code == 2
    assert "--as" in result.output
    publish.assert_not_called()
    create.assert_not_called()


def test_stacked_and_pr_number_are_mutually_exclusive(mocker, tmp_path):
    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--pr", "77"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_review_container_without_a_tty_names_both_flags(mocker, tmp_path):
    _incus, publish, _create, _retarget = _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert "--stacked" in result.output
    publish.assert_not_called()


def test_menu_stacked_choice_opens_the_stacked_pr(mocker, tmp_path):
    _incus, _publish, create, _retarget = _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pr_flow._pick_review_action", return_value="stacked")

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--as", "fix/worktime-review", "--no-retarget"]
    )

    assert result.exit_code == 0, result.output
    assert create.call_args.kwargs["base"] == "contributor/fix-worktime"


def test_rerun_updates_the_stacked_pr_without_asking_again(mocker, tmp_path):
    _incus, publish, create, _retarget = _stacked_setup(
        mocker,
        tmp_path,
        publish_name="fix/worktime-review",
        extra_labels={
            "user.jailbee.stacked_pr": "123",
            "user.jailbee.stacked_pr_branch": "fix/worktime-review",
            "user.jailbee.stacked_pr_author": "1",
            "user.jailbee.stacked_pr_base": "contributor/fix-worktime",
        },
    )
    pick = mocker.patch("jailbee.pr_flow._pick_review_action")

    result = CliRunner().invoke(app, ["pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    pick.assert_not_called()
    create.assert_not_called()
    assert publish.call_args.kwargs["publish_name"] == "fix/worktime-review"


def test_stacked_flag_is_a_no_op_on_an_already_stacked_container(mocker, tmp_path):
    _incus, _publish, create, retarget = _stacked_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.stacked_pr": "123",
            "user.jailbee.stacked_pr_branch": "fix/worktime-review",
            "user.jailbee.stacked_pr_author": "1",
            "user.jailbee.stacked_pr_base": "contributor/fix-worktime",
        },
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked"])

    assert result.exit_code == 0, result.output
    create.assert_not_called()
    retarget.assert_not_called()


def test_stacked_refused_on_an_adopted_container(mocker, tmp_path):
    _incus, publish, create, _retarget = _stacked_setup(
        mocker,
        tmp_path,
        extra_labels={
            "user.jailbee.pr_adopted": "1",
            "user.jailbee.pr_branch": "contributor/fix-worktime",
        },
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked"])

    assert result.exit_code == 2
    assert "already publishes" in result.output
    publish.assert_not_called()
    create.assert_not_called()


def test_stacked_refused_without_a_reviewed_pr(mocker, tmp_path):
    _setup(mocker, tmp_path)
    publish = mocker.patch("jailbee.sync.publish_branch_from_container")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked"])

    assert result.exit_code == 2
    assert "review container" in result.output
    publish.assert_not_called()


def test_stacked_refused_on_a_fork_pr(mocker, tmp_path):
    _incus, publish, create, _retarget = _stacked_setup(
        mocker, tmp_path, pr_info=_review_pr_info(cross=True, owner="contributor")
    )

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--as", "fix/x"])

    assert result.exit_code == 1
    assert "contributor" in result.output
    publish.assert_not_called()
    create.assert_not_called()


def test_stacked_never_renames_the_hosts_copy_of_the_reviewed_branch(mocker, tmp_path):
    """The container's branch is the PR author's branch, and the host may well
    have its own copy. The create path's rename-to-match-the-PR-head step must
    not touch it."""
    _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.git.local_branch_exists", return_value=True)
    rename = mocker.patch("jailbee.git.rename_branch")

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review", "--no-retarget"]
    )

    assert result.exit_code == 0, result.output
    rename.assert_not_called()


def test_stacked_retargets_the_container_base_when_confirmed(mocker, tmp_path):
    _incus, _publish, _create, retarget = _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=True)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review"])

    assert result.exit_code == 0, result.output
    args, kwargs = retarget.call_args.args, retarget.call_args.kwargs
    assert args[3] == "contributor/fix-worktime"
    assert kwargs["source_ref"] == "refs/jailbee/pr/456/head"


def test_stacked_retarget_declined_leaves_the_base_alone(mocker, tmp_path):
    _incus, _publish, _create, retarget = _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review"])

    assert result.exit_code == 0, result.output
    retarget.assert_not_called()


def test_stacked_retarget_skipped_without_a_tty_and_prints_the_command(mocker, tmp_path):
    _incus, _publish, _create, retarget = _stacked_setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review"])

    assert result.exit_code == 0, result.output
    retarget.assert_not_called()
    assert "jailbee git retarget" in result.output


def test_stacked_retarget_failure_does_not_fail_the_publish(mocker, tmp_path):
    from jailbee.sync import SyncError

    _incus, _publish, _create, retarget = _stacked_setup(mocker, tmp_path)
    retarget.side_effect = SyncError("container is not running")

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review", "--retarget"]
    )

    assert result.exit_code == 0, result.output
    assert "not running" in result.output
    assert "pull/123" in result.output


def test_stacked_retarget_skipped_when_the_base_already_matches(mocker, tmp_path):
    _incus, _publish, _create, retarget = _stacked_setup(
        mocker, tmp_path, extra_labels={"user.jailbee.base_branch": "contributor/fix-worktime"}
    )

    result = CliRunner().invoke(
        app, ["pr", "feat-foo", "--stacked", "--as", "fix/worktime-review", "--retarget"]
    )

    assert result.exit_code == 0, result.output
    retarget.assert_not_called()


def test_open_prefers_the_stacked_pr(mocker, tmp_path):
    _setup(
        mocker,
        tmp_path,
        labels={
            "user.jailbee.pr": "456",
            "user.jailbee.stacked_pr": "123",
            "user.jailbee.stacked_pr_branch": "fix/worktime-review",
        },
    )
    open_browser = mocker.patch("jailbee.pr.open_pr_in_browser")

    result = CliRunner().invoke(app, ["pr", "feat-foo", "--open"])

    assert result.exit_code == 0, result.output
    open_browser.assert_called_once_with(tmp_path, 123)
