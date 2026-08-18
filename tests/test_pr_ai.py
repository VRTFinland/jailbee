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
    assert "claude -p" in shell_cmd
    assert "--output-format json" in shell_cmd
    assert "--dangerously-skip-permissions" in shell_cmd
    # prompt is passed via env var, never interpolated into the shell string
    assert "$JAILBEE_PR_PROMPT" in shell_cmd
    assert call.kwargs["uid"] == cfg.container_user.uid
    assert call.kwargs["gid"] == cfg.container_user.gid
    assert call.kwargs["cwd"] == "/home/dev/repo"
    assert call.kwargs["timeout"] == 180
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


def test_generate_returns_none_on_timeout(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.pr_ai import generate_pr_text

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("`incus exec c` timed out after 180s")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    assert generate_pr_text(cfg, incus, "c", branch="feat/foo", base="main") is None


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
    assert "claude -p" in argv[2]
    # prompt passed via env, never interpolated into the shell string
    assert "$JAILBEE_PR_PROMPT" in argv[2]
    assert incus.exec.call_args.kwargs["env"]["JAILBEE_PR_PROMPT"]
