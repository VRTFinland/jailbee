"""Unit tests for jailbee.pr_ai (in-container Claude PR-text bridge)."""

from __future__ import annotations

import json


def _envelope(inner_text: str) -> str:
    """Wrap model text in Claude's --output-format json envelope."""
    return json.dumps({"type": "result", "result": inner_text})


# ---------------------------------------------------------------------------
# _parse_pr_text
# ---------------------------------------------------------------------------


def test_parse_plain_inner_json():
    from jailbee.pr_ai import PrText, _parse_pr_text

    inner = json.dumps({"title": "feat: do x", "body": "Background.\n\nChanged y."})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") == PrText(
        title="feat: do x", body="Background.\n\nChanged y.", branch="dev-1"
    )


def test_parse_fenced_inner_json():
    from jailbee.pr_ai import _parse_pr_text

    inner = '```json\n{"title": "t", "body": "b"}\n```'
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.title == "t"
    assert result.body == "b"


def test_parse_first_brace_slice_fallback():
    from jailbee.pr_ai import _parse_pr_text

    inner = 'Sure! Here it is:\n{"title": "t", "body": "b"}\nHope that helps.'
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.title == "t"


def test_parse_bare_stdout_without_envelope():
    from jailbee.pr_ai import _parse_pr_text

    # stdout is not the Claude envelope — treat the whole thing as candidate.
    bare = json.dumps({"title": "t", "body": "b"})
    result = _parse_pr_text(bare, None, None, current_branch="dev-1")
    assert result is not None
    assert result.title == "t"


def test_parse_rejects_empty_title():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "  ", "body": "b"})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") is None


def test_parse_rejects_missing_body():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "t"})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") is None


def test_parse_rejects_oversized_title():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "x" * 121, "body": "b"})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") is None


def test_parse_rejects_garbage():
    from jailbee.pr_ai import _parse_pr_text

    assert _parse_pr_text("not json at all", None, None, current_branch="dev-1") is None


def test_parse_fixed_title_overrides_model_echo():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "model wrote this", "body": "model body"})
    result = _parse_pr_text(_envelope(inner), "MY FIXED TITLE", None, current_branch="dev-1")
    assert result is not None
    assert result.title == "MY FIXED TITLE"
    assert result.body == "model body"


def test_parse_trailing_prose_with_brace_does_not_break():
    from jailbee.pr_ai import _parse_pr_text

    inner = 'Here it is:\n{"title": "t", "body": "see foo()"}\nNote: the } above.'
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.title == "t"
    assert result.body == "see foo()"


def test_parse_leading_prose_with_stray_brace():
    from jailbee.pr_ai import _parse_pr_text

    inner = 'I think { not really } here:\n{"title": "t", "body": "b"}'
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.title == "t"
    assert result.body == "b"


def test_parse_brace_inside_string_body():
    from jailbee.pr_ai import _parse_pr_text

    # A literal } inside the body string must not end the object early.
    inner = '{"title": "t", "body": "code: if (x) { return } end"}'
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.body == "code: if (x) { return } end"


def test_parse_fixed_body_overrides_model_echo():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "model title", "body": "model wrote this"})
    result = _parse_pr_text(_envelope(inner), None, "MY FIXED BODY", current_branch="dev-1")
    assert result is not None
    assert result.title == "model title"
    assert result.body == "MY FIXED BODY"


def test_parse_includes_branch(mocker):
    from jailbee.pr_ai import PrText, _parse_pr_text

    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    inner = json.dumps({"title": "t", "body": "b", "branch": "feat/nice"})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") == PrText(
        title="t", body="b", branch="feat/nice"
    )


def test_parse_missing_branch_falls_back_to_current(mocker):
    from jailbee.pr_ai import _parse_pr_text

    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    inner = json.dumps({"title": "t", "body": "b"})
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.branch == "dev-1"


def test_parse_invalid_branch_falls_back_to_current(mocker):
    from jailbee.pr_ai import _parse_pr_text

    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    inner = json.dumps({"title": "t", "body": "b", "branch": "bad..name"})
    result = _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1")
    assert result is not None
    assert result.branch == "dev-1"


def test_parse_bad_titlebody_still_none_even_with_branch():
    from jailbee.pr_ai import _parse_pr_text

    inner = json.dumps({"title": "", "body": "b", "branch": "feat/nice"})
    assert _parse_pr_text(_envelope(inner), None, None, current_branch="dev-1") is None


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_prompt_points_at_the_repo_pr_template():
    from jailbee.pr_ai import _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None)

    assert "pull_request_template" in prompt


def test_prompt_asks_for_a_closes_line_when_an_issue_is_referenced():
    from jailbee.pr_ai import _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None)

    assert "gh issue view" in prompt
    assert "Closes #" in prompt


def test_prompt_asks_what_the_change_leaves_out_relative_to_its_spec():
    from jailbee.pr_ai import _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None)

    assert "leaves out" in prompt


def test_prompt_forbids_running_the_projects_test_suite():
    """The run has a fixed timeout but the test suite's cost is the repo's.

    Without this clause the model answers "how it was tested" by running the
    suite — measured at 59s of a 165s run in this repo, and more than the whole
    180s budget in a larger one.
    """
    from jailbee.pr_ai import _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None)

    assert "Do NOT run" in prompt
    for forbidden in ("tests", "build", "linters", "installers"):
        assert forbidden in prompt.split("Do NOT run", 1)[1]


def test_prompt_cost_guard_precedes_the_project_block():
    """A project that really wants its suite run can say so in `pr_prompt`.

    The project block outranks the generic guidance, so the guard has to sit
    above it for that override to be possible — and it must stay above the JSON
    contract, which nothing may override.
    """
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None, project_prompt="Run the suite.")

    assert prompt.index("Do NOT run") < prompt.index(_PROJECT_BLOCK_HEADER)
    assert prompt.index(_PROJECT_BLOCK_HEADER) < prompt.index('{"title"')


def test_prompt_has_no_project_block_without_a_configured_pr_prompt():
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None)

    assert _PROJECT_BLOCK_HEADER not in prompt


def test_prompt_embeds_the_project_prompt_verbatim():
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt(
        "feat/foo", "main", None, None, project_prompt="## Motivation\n## Risk\n"
    )

    assert _PROJECT_BLOCK_HEADER in prompt
    assert "## Motivation\n## Risk\n" in prompt


def test_project_instructions_are_declared_to_win_over_the_generic_rules():
    from jailbee.pr_ai import _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None, project_prompt="Use our template.")

    assert "THESE WIN" in prompt


def test_project_block_precedes_the_json_response_contract():
    """The output contract stays last so project instructions can't displace it."""
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None, project_prompt="Use our template.")

    assert prompt.index(_PROJECT_BLOCK_HEADER) < prompt.index("ONLY a JSON object")


def test_whitespace_only_project_prompt_is_treated_as_absent():
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt("feat/foo", "main", None, None, project_prompt="  \n\t\n")

    assert _PROJECT_BLOCK_HEADER not in prompt


def test_project_prompt_and_fixed_title_clause_coexist():
    from jailbee.pr_ai import _PROJECT_BLOCK_HEADER, _build_prompt

    prompt = _build_prompt(
        "feat/foo", "main", "feat: fixed", None, project_prompt="Use our template."
    )

    assert _PROJECT_BLOCK_HEADER in prompt
    assert "feat: fixed" in prompt


# ---------------------------------------------------------------------------
# generate_pr_text
# ---------------------------------------------------------------------------


def test_generate_happy_path_builds_exec_and_parses(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import PrText, generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    inner = json.dumps({"title": "feat: thing", "body": "did the thing"})
    incus.exec.return_value = _envelope(inner)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    result = generate_pr_text(cfg, incus, "sampleapp-feat-foo", branch="feat/foo", base="main")

    assert result == PrText(title="feat: thing", body="did the thing", branch="feat/foo")
    incus.exec.assert_called_once()
    call = incus.exec.call_args
    assert call.args[0] == "sampleapp-feat-foo"
    argv = call.args[1]
    # Invoked as a login shell so ~/.local/bin is on PATH
    assert argv[0] == "bash"
    assert argv[1] == "-lc"
    shell_cmd = argv[2]
    assert shell_cmd.startswith("claude ")
    assert '-p "$JAILBEE_PR_PROMPT"' in shell_cmd
    assert "--output-format json" in shell_cmd
    assert "--dangerously-skip-permissions" in shell_cmd
    # prompt is passed via env var, never interpolated into the shell string
    assert "$JAILBEE_PR_PROMPT" in shell_cmd
    assert call.kwargs["uid"] == cfg.container_user.uid
    assert call.kwargs["gid"] == cfg.container_user.gid
    assert call.kwargs["cwd"] == "/home/dev/repo"
    assert call.kwargs["timeout"] == cfg.claude.ai_pr_timeout
    # base branch and feature branch name appear in the env prompt
    env_prompt = call.kwargs["env"]["JAILBEE_PR_PROMPT"]
    assert "main" in env_prompt
    assert "feat/foo" in env_prompt
    # HOME is set in env
    assert call.kwargs["env"]["HOME"]


def test_generate_returns_none_on_incus_error(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("exit 127: claude: not found")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None


def test_generate_reports_why_the_container_claude_failed(mocker, make_cfg, tmp_path):
    """Without the reason, a bad ai_pr_model reads as an unexplained failure."""
    from jailbee.incus import IncusError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("exit 1: error: unknown model 'sonnnet'")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    warn = mocker.patch("jailbee.pr_ai.warn")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None

    warn.assert_called_once()
    assert "unknown model 'sonnnet'" in warn.call_args.args[0]


def test_generate_returns_none_on_timeout(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusTimeoutError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusTimeoutError("`incus exec c` timed out after 600s")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None


def test_generate_pins_the_session_id_it_can_later_name(mocker, make_cfg, tmp_path):
    """Read from the reply it would be unavailable exactly when it is needed.

    `--output-format json` emits nothing until the run ends, so on a timeout the
    reply — and the session id inside it — never arrive. Pinning it up front is
    what lets the timeout warning point at a transcript.
    """
    import uuid as uuid_mod

    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    call = incus.exec.call_args
    assert '--session-id "$JAILBEE_PR_SESSION"' in call.args[1][2]
    # A real UUID: `claude --session-id` rejects anything else.
    uuid_mod.UUID(call.kwargs["env"]["JAILBEE_PR_SESSION"])


def test_timeout_warning_names_the_container_session_and_budget(mocker, make_cfg, tmp_path):
    """A timeout used to be a dead end: no bytes, no hint that a transcript exists."""
    from jailbee.incus import IncusTimeoutError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusTimeoutError("timed out after 600s")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    warn = mocker.patch("jailbee.pr_ai.warn")

    full = f"{cfg.container_prefix}-feat-foo"
    assert generate_pr_text(cfg, incus, full, branch="feat/foo", base="main") is None

    hint = " ".join(c.args[0] for c in warn.call_args_list)
    session_id = incus.exec.call_args.kwargs["env"]["JAILBEE_PR_SESSION"]
    assert f"claude --resume {session_id}" in hint
    assert "jailbee shell feat-foo" in hint  # short name, not the prefixed one
    assert f"{cfg.claude.ai_pr_timeout}s" in hint


def test_non_timeout_failure_does_not_promise_a_transcript(mocker, make_cfg, tmp_path):
    """A missing `claude` or a rejected model leaves nothing to resume.

    Sending the user into the container after a session that was never created
    is worse than saying nothing.
    """
    from jailbee.incus import IncusError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("bash: line 1: claude: command not found")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    warn = mocker.patch("jailbee.pr_ai.warn")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None

    everything = " ".join(c.args[0] for c in warn.call_args_list)
    assert "--resume" not in everything
    assert "transcript" not in everything


def test_generate_returns_none_on_unparseable_output(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope("the model refused to follow the format")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None


def test_generate_forwards_custom_timeout(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main", timeout=42)

    assert incus.exec.call_args.kwargs["timeout"] == 42


def test_generate_uses_the_configured_ai_pr_timeout(mocker, make_cfg, tmp_path):
    """The budget has to come from config, not from a literal in this module.

    It was hard-coded at 180s and neither `cli.py` call site passed a value, so
    a repository whose generation legitimately needs longer had no knob at all
    — the only symptom was a timeout warning and a placeholder description.
    """
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(update={"claude": cfg.claude.model_copy(update={"ai_pr_timeout": 900})})
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    assert incus.exec.call_args.kwargs["timeout"] == 900


def test_generate_runs_through_login_shell(mocker, make_cfg, tmp_path):
    import json

    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = json.dumps(
        {"type": "result", "result": json.dumps({"title": "t", "body": "b"})}
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    argv = incus.exec.call_args.args[1]
    assert argv[0] == "bash"
    assert argv[1] == "-lc"
    assert argv[2].startswith("claude ")
    # prompt passed via env, never interpolated into the shell string
    assert '-p "$JAILBEE_PR_PROMPT"' in argv[2]
    assert incus.exec.call_args.kwargs["env"]["JAILBEE_PR_PROMPT"]


def test_generate_selects_the_configured_model_via_env(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={"claude": cfg.claude.model_copy(update={"ai_pr_model": "claude-haiku-4-5"})}
    )
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    shell_cmd = incus.exec.call_args.args[1][2]
    assert '${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"}' in shell_cmd
    # the model name goes through the environment, never into the shell string
    assert "claude-haiku-4-5" not in shell_cmd
    assert incus.exec.call_args.kwargs["env"]["JAILBEE_PR_MODEL"] == "claude-haiku-4-5"


def test_generate_drops_the_model_flag_when_ai_pr_model_is_null(mocker, make_cfg, tmp_path):
    """An empty env var makes the `${VAR:+...}` expansion vanish entirely."""
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(update={"claude": cfg.claude.model_copy(update={"ai_pr_model": None})})
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    assert incus.exec.call_args.kwargs["env"]["JAILBEE_PR_MODEL"] == ""


def test_generate_threads_configured_pr_prompt_into_the_prompt(mocker, make_cfg, tmp_path):
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "claude": cfg.claude.model_copy(update={"pr_prompt": "Always mention the JIRA id."})
        }
    )
    incus = mocker.MagicMock()
    incus.exec.return_value = _envelope(json.dumps({"title": "t", "body": "b"}))
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main")

    env_prompt = incus.exec.call_args.kwargs["env"]["JAILBEE_PR_PROMPT"]
    assert "Always mention the JIRA id." in env_prompt
