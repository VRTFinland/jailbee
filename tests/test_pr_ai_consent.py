"""Agent runs require consent independently of publication confirmation."""

import pytest
from typer.testing import CliRunner

from jailbee import pr_flow
from jailbee.cli import app
from jailbee.pr_ai import PrText
from tests.conftest import make_cfg


@pytest.mark.parametrize("answer", [False, True])
def test_creation_asks_once_for_both_features(tmp_path, mocker, answer):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=answer)
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    generate = mocker.patch(
        "jailbee.pr_ai.generate_pr_text", return_value=PrText("Title", "Body", "ai/head")
    )
    plan = pr_flow.resolve_pr_text_and_head(
        cfg,
        mocker.MagicMock(),
        "c",
        pr_flow.PrScope.for_repo(cfg),
        is_update=False,
        stored_head=None,
        source_branch="feat/x",
        base="main",
        title=None,
        body=None,
        as_name=None,
        no_ai=False,
        status_label="Generating",
    )
    confirm.assert_called_once()
    question, default = confirm.call_args.args
    assert "claude" in question.lower()
    assert "title and description" in question
    assert "branch" in question
    assert default is False
    assert generate.call_count == int(answer)
    assert plan.publish_name == ("ai/head" if answer else "feat/x")


def test_creation_noninteractive_skips_with_stderr_hint(tmp_path, mocker, capsys):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text")
    plan = pr_flow.resolve_pr_text_and_head(
        cfg,
        mocker.MagicMock(),
        "c",
        pr_flow.PrScope.for_repo(cfg),
        is_update=False,
        stored_head=None,
        source_branch="feat/x",
        base="main",
        title=None,
        body=None,
        as_name=None,
        no_ai=False,
        status_label="Generating",
    )
    generate.assert_not_called()
    assert plan.ai_text is None
    assert "--ai" in capsys.readouterr().err


@pytest.mark.parametrize(
    "description,branch,phrase",
    [(True, False, "title and description"), (False, True, "branch-name proposal")],
)
def test_consent_names_only_enabled_features(tmp_path, mocker, description, branch, phrase):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=True)
    consent = pr_flow.AiConsent()
    assert consent.allow(cfg, description=description, branch=branch)
    assert phrase in confirm.call_args.args[0]
    assert ("title and description" in confirm.call_args.args[0]) == description
    assert ("branch-name proposal" in confirm.call_args.args[0]) == branch
    assert consent.allow(cfg, description=description, branch=branch)
    confirm.assert_called_once()


@pytest.mark.parametrize("ai,no_ai,expected", [(True, False, True), (False, True, False)])
def test_explicit_choice_never_prompts(tmp_path, mocker, ai, no_ai, expected):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    confirm = mocker.patch("jailbee.prompting._confirm")
    assert (
        pr_flow.AiConsent(ai=ai, no_ai=no_ai).allow(cfg, description=True, branch=True) is expected
    )
    confirm.assert_not_called()


@pytest.mark.parametrize("submodule", [False, True], ids=["repo", "submodule"])
@pytest.mark.parametrize(
    "mode,answer,expected",
    [
        ("ask", False, 0),
        ("ask", True, 1),
        ("--ai", False, 1),
        ("--no-ai", True, 0),
        ("noninteractive", True, 0),
        ("disabled", True, 0),
        ("outbox", True, 0),
    ],
)
def test_cli_agent_run_requires_consent(tmp_path, mocker, submodule, mode, answer, expected):
    if submodule:
        from tests.test_cli_submodule_pr import _happy, _setup

        cfg, _, _ = _setup(mocker, tmp_path)
        _happy(mocker)
        args = ["submodule", "pr", "push", "feat-foo", "lib/a", "--yes"]
    else:
        from tests.test_cli_pr import _pr_created, _publish_result, _setup

        cfg, _ = _setup(mocker, tmp_path)
        mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
        mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
        args = ["pr", "push", "feat-foo", "--no-submodules", "--yes"]
    from tests.conftest import mock_pr_agent

    mock_pr_agent(cfg, True)
    cfg.pr.ai_description = mode != "disabled"
    cfg.pr.ai_branch = mode != "disabled"
    mocker.patch("jailbee.git.commit_subject", return_value="Fallback title")
    mocker.patch("jailbee.prompting.is_interactive", return_value=mode != "noninteractive")
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=answer)
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    generate = mocker.patch(
        "jailbee.pr_ai.generate_pr_text", return_value=PrText("Title", "Body", "feat/foo")
    )
    if mode == "outbox":
        from tests.test_cli_submodule_pr import _outbox_source

        mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=_outbox_source())
        mocker.patch("jailbee.pr_outbox.record_consumed")
    if mode.startswith("--"):
        args.append(mode)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert generate.call_count == expected
    assert confirm.call_count == int(mode == "ask")
    if mode == "noninteractive":
        assert "--ai" in result.stderr


@pytest.mark.parametrize("submodule", [False, True])
def test_conflicting_ai_flags_fail_before_publication(mocker, submodule):
    load = mocker.patch("jailbee.cli._load_or_exit")
    args = (["submodule"] if submodule else []) + ["pr", "push", "feat-foo", "--ai", "--no-ai"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    load.assert_not_called()


@pytest.mark.parametrize("text", [None, PrText("Generated", "Body", "feat/x")])
def test_discovered_existing_pr_does_not_repeat_agent_run(tmp_path, mocker, text):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    scope = pr_flow.PrScope.for_repo(cfg)
    consent = pr_flow.AiConsent()
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("jailbee.prompting._confirm", return_value=True)
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text", return_value=text)
    mocker.patch("jailbee.pr_flow.confirm_pr_branch_name", side_effect=lambda p, s: p)
    incus = mocker.MagicMock()
    plan = pr_flow.resolve_pr_text_and_head(
        cfg, incus, "c", scope, is_update=False, stored_head=None,
        source_branch="feat/x", base="main", title=None, body=None, as_name=None,
        no_ai=False, status_label="Generating", consent=consent,
    )
    update = pr_flow.resolve_pr_description_update(
        cfg, incus, "c", scope, branch="feat/x", base="main", title=None,
        body=None, description=True, ai_on=True, consent=consent, generated_text=plan.ai_text,
    )
    generate.assert_called_once()
    confirm.assert_called_once()
    assert (update is not None) == (text is not None)


def test_noninteractive_description_flag_is_not_agent_consent(tmp_path, mocker, capsys):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    generate = mocker.patch("jailbee.pr_ai.generate_pr_text")
    update = pr_flow.resolve_pr_description_update(
        cfg, mocker.MagicMock(), "c", pr_flow.PrScope.for_repo(cfg), branch="feat/x",
        base="main", title=None, body=None, description=True, ai_on=True,
    )
    assert update is None
    generate.assert_not_called()
    assert "--ai" in capsys.readouterr().err
