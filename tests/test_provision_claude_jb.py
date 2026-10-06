"""Runs the `claude-jb` script install.sh writes, against a fake `claude`
that prints its environment and argv. Needs bash and jq (both in the golden
image and in the dev environment)."""

import importlib.resources
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_MARKER = "cat > /usr/local/bin/claude-jb <<'EOF'\n"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

PAYLOAD = {
    "version": 1,
    "default_profile": "codex",
    "profiles": {
        "codex": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "KEYFILE",
            "effort": None,
            "tiers": {"opus": "jb-default-sol-high", "haiku": "jb-default-luna-high"},
            "context_window": 272000,
            "max_context_window": 1050000,
        },
        "deep": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "KEYFILE",
            "effort": "max",
            "tiers": {"opus": "jb-default-astra"},
            "context_window": 272000,
            "max_context_window": 272000,
        },
        # Written by a jailbee that predates `max_context_window`.
        "old": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "KEYFILE",
            "effort": None,
            "tiers": {"opus": "jb-default-astra"},
            "context_window": 272000,
        },
    },
}


@pytest.fixture
def script(tmp_path: Path) -> Path:
    text = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    assert _MARKER in text
    start = text.index(_MARKER) + len(_MARKER)
    body = text[start : text.index("\nEOF\n", start)]
    path = tmp_path / "claude-jb"
    path.write_text(body + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/bash\n"
        "env | grep -E '^(ANTHROPIC|CLAUDE_CODE)_' | sort\n"
        'printf "ARGV:%s\\n" "$@"\n'
    )
    fake.chmod(0o755)
    key = tmp_path / "key"
    key.write_text("sk-jb-secret\n")
    cfg = tmp_path / "litellm.json"
    cfg.write_text(json.dumps(PAYLOAD).replace("KEYFILE", str(key)))
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "JAILBEE_LITELLM_CONFIG": str(cfg),
        # Fake claude prints its environment; no proxy runs in these tests.
        "JAILBEE_LITELLM_SKIP_REACHABILITY": "1",
    }


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script), *args], env=env, capture_output=True, text=True)


def _lines(out: str) -> list[str]:
    return out.splitlines()


def test_default_profile_env(script, env):
    r = _run(script, env, "-p", "hi")
    assert r.returncode == 0, r.stderr
    lines = _lines(r.stdout)
    assert "ANTHROPIC_BASE_URL=http://10.0.0.3:4100" in lines
    assert "ANTHROPIC_AUTH_TOKEN=sk-jb-secret" in lines
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-sol-high" in lines
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL=jb-default-luna-high" in lines
    assert not any(line.startswith("ANTHROPIC_DEFAULT_SONNET_MODEL=") for line in lines)
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000" in lines
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1" in lines
    assert [line for line in lines if line.startswith("ARGV:")] == ["ARGV:-p", "ARGV:hi"]


def test_profile_flag_is_stripped_and_selects(script, env):
    r = _run(script, env, "--profile", "deep", "-p", "hi")
    lines = _lines(r.stdout)
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in lines
    assert [line for line in lines if line.startswith("ARGV:")] == [
        "ARGV:--effort",
        "ARGV:max",
        "ARGV:-p",
        "ARGV:hi",
    ]


def test_profile_equals_form(script, env):
    r = _run(script, env, "--profile=deep")
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in _lines(r.stdout)


def test_explicit_empty_profile_is_rejected(script, env):
    r = _run(script, env, "--profile=")
    assert r.returncode != 0 and "--profile needs a name" in r.stderr
    assert "ANTHROPIC_AUTH_TOKEN" not in r.stdout


def test_env_var_selects_profile(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_PROFILE": "deep"})
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in _lines(r.stdout)


def test_flag_beats_env_var(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_PROFILE": "deep"}, "--profile", "codex")
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-sol-high" in _lines(r.stdout)


def test_user_effort_wins_over_profile_effort(script, env):
    r = _run(script, env, "--profile", "deep", "--effort", "low")
    argv = [line for line in _lines(r.stdout) if line.startswith("ARGV:")]
    assert argv == ["ARGV:--effort", "ARGV:low"]


def test_user_effort_equals_form_also_wins(script, env):
    r = _run(script, env, "--profile", "deep", "--effort=low")
    argv = [line for line in _lines(r.stdout) if line.startswith("ARGV:")]
    assert argv == ["ARGV:--effort=low"]


def test_unknown_profile_errors(script, env):
    r = _run(script, env, "--profile", "nope")
    assert r.returncode != 0
    assert "unknown profile 'nope'" in r.stderr and "codex" in r.stderr


def test_missing_config_errors_with_fix(script, env, tmp_path):
    r = _run(script, {**env, "JAILBEE_LITELLM_CONFIG": str(tmp_path / "absent.json")})
    assert r.returncode != 0
    assert "jailbee litellm up" in r.stderr and "jailbee apply" in r.stderr


def test_corrupt_config_errors(script, env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    r = _run(script, {**env, "JAILBEE_LITELLM_CONFIG": str(bad)})
    assert r.returncode != 0
    assert "cannot read" in r.stderr


def test_unreadable_key_errors(script, env, tmp_path):
    cfg = json.loads(Path(env["JAILBEE_LITELLM_CONFIG"]).read_text())
    cfg["profiles"]["codex"]["key_file"] = str(tmp_path / "nokey")
    Path(env["JAILBEE_LITELLM_CONFIG"]).write_text(json.dumps(cfg))
    r = _run(script, env)
    assert r.returncode != 0 and "key" in r.stderr


def test_empty_readable_key_errors_before_claude(script, env):
    cfg = json.loads(Path(env["JAILBEE_LITELLM_CONFIG"]).read_text())
    Path(cfg["profiles"]["codex"]["key_file"]).write_text("\n")
    r = _run(script, env)
    assert r.returncode != 0 and "empty" in r.stderr and "jailbee apply" in r.stderr
    assert "ARGV:" not in r.stdout


def test_unreachable_gateway_errors_before_claude(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_SKIP_REACHABILITY": ""})
    assert r.returncode != 0 and "jailbee litellm up" in r.stderr
    assert "jailbee apply" in r.stderr and "ARGV:" not in r.stdout


def test_inherited_anthropic_vars_are_replaced(script, env):
    r = _run(script, {**env, "ANTHROPIC_DEFAULT_SONNET_MODEL": "stale", "ANTHROPIC_API_KEY": "x"})
    lines = _lines(r.stdout)
    assert not any(line.startswith("ANTHROPIC_DEFAULT_SONNET_MODEL=") for line in lines)
    assert not any(line.startswith("ANTHROPIC_API_KEY=") for line in lines)


@pytest.fixture
def argv_env(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """`env` with a fake `claude` that prints its argv as one JSON array, so an
    argument with newlines in it survives the round trip. The `--` stops jq
    reading `-p` and friends as its own options (it drops only the first one)."""
    fake = tmp_path / "bin" / "claude"
    fake.write_text("#!/bin/bash\njq -cn '$ARGS.positional' --args -- \"$@\"\n")
    fake.chmod(0o755)
    return env


def _set_instructions(env: dict[str, str], text: str | None, profile: str = "codex") -> None:
    path = Path(env["JAILBEE_LITELLM_CONFIG"])
    cfg = json.loads(path.read_text())
    cfg["profiles"][profile]["instructions"] = text
    path.write_text(json.dumps(cfg))


def _argv(r: subprocess.CompletedProcess[str]) -> list[str]:
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


APPEND = "--append-system-prompt"
POLICY = "Never use fable.\nPrefer haiku for lookups."


def test_a_litellm_json_without_the_key_adds_no_flag(script, argv_env):
    """The fixture payload predates `instructions`: an older file must keep working."""
    assert _argv(_run(script, argv_env, "-p", "hi")) == ["-p", "hi"]


def test_a_null_instructions_value_adds_no_flag(script, argv_env):
    _set_instructions(argv_env, None)
    assert _argv(_run(script, argv_env, "-p", "hi")) == ["-p", "hi"]


def test_profile_instructions_become_one_append_flag(script, argv_env):
    _set_instructions(argv_env, POLICY)
    assert _argv(_run(script, argv_env, "-p", "hi")) == [APPEND, POLICY, "-p", "hi"]


def test_instructions_belong_to_their_profile(script, argv_env):
    _set_instructions(argv_env, POLICY, profile="codex")
    assert _argv(_run(script, argv_env, "--profile", "deep", "-p", "hi")) == [
        "--effort",
        "max",
        "-p",
        "hi",
    ]


def test_the_append_flag_follows_the_profile_effort(script, argv_env):
    _set_instructions(argv_env, POLICY, profile="deep")
    assert _argv(_run(script, argv_env, "--profile", "deep", "-p", "hi")) == [
        "--effort",
        "max",
        APPEND,
        POLICY,
        "-p",
        "hi",
    ]


def test_user_append_text_is_merged_after_the_profile_text(script, argv_env):
    _set_instructions(argv_env, POLICY)
    inline = _argv(_run(script, argv_env, APPEND, "mine", "-p", "hi"))
    equals = _argv(_run(script, argv_env, f"{APPEND}=mine", "-p", "hi"))
    assert inline == equals == [APPEND, f"{POLICY}\n\nmine", "-p", "hi"]


def test_user_append_file_is_read_and_merged(script, argv_env, tmp_path):
    _set_instructions(argv_env, POLICY)
    extra = tmp_path / "extra.md"
    extra.write_text("from the file\n")
    spaced = _argv(_run(script, argv_env, f"{APPEND}-file", str(extra), "-p", "hi"))
    equals = _argv(_run(script, argv_env, f"{APPEND}-file={extra}", "-p", "hi"))
    assert spaced == equals == [APPEND, f"{POLICY}\n\nfrom the file", "-p", "hi"]


def test_user_append_parts_keep_the_order_given(script, argv_env, tmp_path):
    _set_instructions(argv_env, POLICY)
    extra = tmp_path / "extra.md"
    extra.write_text("file part")
    argv = _argv(_run(script, argv_env, f"{APPEND}-file", str(extra), APPEND, "text part"))
    assert argv == [APPEND, f"{POLICY}\n\nfile part\n\ntext part"]


def test_an_unreadable_append_file_is_an_error_when_the_profile_has_text(
    script, argv_env, tmp_path
):
    _set_instructions(argv_env, POLICY)
    r = _run(script, argv_env, f"{APPEND}-file", str(tmp_path / "nope.md"))
    assert r.returncode == 2 and "cannot read" in r.stderr
    assert r.stdout == ""


def test_user_append_flags_pass_through_untouched_without_profile_text(script, argv_env):
    """Nothing to merge: not even an unreadable file is the wrapper's business."""
    args = [APPEND, "a", f"{APPEND}-file", "/does/not/exist", f"{APPEND}=b", "-p", "hi"]
    assert _argv(_run(script, argv_env, *args)) == args


def test_a_dangling_append_flag_is_not_consumed(script, argv_env):
    assert _argv(_run(script, argv_env, "-p", "hi", APPEND)) == ["-p", "hi", APPEND]


def test_double_dash_ends_option_parsing(script, argv_env):
    tail = ["--", "--effort", "low", "--profile", "nope", APPEND, "x"]
    without = _argv(_run(script, argv_env, "--profile", "deep", *tail))
    assert without == ["--effort", "max", *tail]

    _set_instructions(argv_env, POLICY, profile="deep")
    with_text = _argv(_run(script, argv_env, "--profile", "deep", *tail))
    assert with_text == ["--effort", "max", APPEND, POLICY, *tail]


def test_instructions_reach_claude_verbatim(script, argv_env, tmp_path):
    nasty = "$(touch pwned) `touch pwned2` \"q\" 'q' --effort -- * \\n\n  indented\n"
    _set_instructions(argv_env, nasty)
    r = subprocess.run(
        [str(script), "-p", "hi"], env=argv_env, cwd=tmp_path, capture_output=True, text=True
    )
    assert _argv(r) == [APPEND, nasty.rstrip("\n"), "-p", "hi"]
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("a" * 131_071, True),
        ("a" * 131_072, False),
        ("ä" * 65_535, True),  # 131070 bytes
        ("ä" * 65_536, False),  # 131072 bytes: bytes, not characters
    ],
)
def test_the_combined_argument_is_limited_to_one_linux_argv_string(script, argv_env, text, ok):
    _set_instructions(argv_env, text)
    r = _run(script, argv_env, "-p", "hi")
    if ok:
        assert _argv(r) == [APPEND, text, "-p", "hi"]
    else:
        assert r.returncode == 2 and "128 KiB" in r.stderr and r.stdout == ""


def test_profile_text_plus_user_text_over_the_limit_is_refused(script, argv_env):
    _set_instructions(argv_env, "a" * 100_000)
    r = _run(script, argv_env, APPEND, "b" * 40_000)
    assert r.returncode == 2 and "128 KiB" in r.stderr


def _window(r: subprocess.CompletedProcess[str]) -> str:
    assert r.returncode == 0, r.stderr
    [line] = [x for x in _lines(r.stdout) if x.startswith("CLAUDE_CODE_MAX_CONTEXT_TOKENS=")]
    return line.split("=", 1)[1]


@pytest.mark.parametrize(
    ("args", "window"),
    [
        (["--context", "1m"], "1000000"),
        (["--context=1m"], "1000000"),
        (["-C", "272k"], "272000"),
        (["-C", "500000"], "500000"),
        (["--context", "max"], "1050000"),
        (["--context", "default"], "272000"),
        (["--context", "1M"], "1000000"),
    ],
)
def test_context_flag_sets_the_window(script, env, args, window):
    assert _window(_run(script, env, *args)) == window


def test_context_flag_is_stripped_from_claude_argv(script, env):
    r = _run(script, env, "-C", "1m", "-p", "hi", "--context=272k")
    assert [x for x in _lines(r.stdout) if x.startswith("ARGV:")] == ["ARGV:-p", "ARGV:hi"]


def test_the_last_context_flag_wins(script, env):
    assert _window(_run(script, env, "-C", "272k", "-C", "1m")) == "1000000"


def test_lowercase_c_is_claudes_continue_and_passes_through(script, argv_env):
    assert _argv(_run(script, argv_env, "-c", "-p", "hi")) == ["-c", "-p", "hi"]


def test_a_value_above_the_profile_ceiling_is_refused(script, env):
    r = _run(script, env, "--profile", "deep", "-C", "1m")
    assert r.returncode == 2 and r.stdout == ""
    assert "272000" in r.stderr and "deep" in r.stderr


def test_the_ceiling_itself_is_allowed(script, env):
    assert _window(_run(script, env, "-C", "1050000")) == "1050000"
    r = _run(script, env, "-C", "1050001")
    assert r.returncode == 2 and r.stdout == ""


@pytest.mark.parametrize("value", ["banana", "0", "-5", "1g", "k", "", "1 m"])
def test_an_unparseable_context_is_refused(script, env, value):
    r = _run(script, env, "--context", value)
    assert r.returncode == 2 and r.stdout == ""
    assert "--context" in r.stderr


def test_a_context_flag_without_a_value_is_refused(script, env):
    r = _run(script, env, "-p", "hi", "--context")
    assert r.returncode == 2 and "--context needs a value" in r.stderr


def test_a_profile_without_a_ceiling_allows_only_its_default(script, env):
    assert _window(_run(script, env, "--profile", "old", "-C", "272k")) == "272000"
    r = _run(script, env, "--profile", "old", "-C", "1m")
    assert r.returncode == 2 and "jailbee apply" in r.stderr and r.stdout == ""


def test_context_flag_after_double_dash_is_not_ours(script, argv_env):
    tail = ["--", "-C", "1m"]
    assert _argv(_run(script, argv_env, *tail)) == tail


def test_a_larger_window_prints_a_cost_note_on_stderr(script, env):
    r = _run(script, env, "-C", "1m")
    assert "1000000" in r.stderr and "cost" in r.stderr
    assert r.stderr == _run(script, env, "-C", "max").stderr.replace("1050000", "1000000")
    assert _run(script, env, "-C", "272k").stderr == ""
    assert _run(script, env).stderr == ""


def test_help_prints_claude_jb_options_then_claudes_own_help(script, env):
    r = _run(script, env, "--help")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("claude-jb") < out.index("ARGV:--help")
    for needle in ("--profile", "--context", "-C", "codex", "deep", "272000", "1050000"):
        assert needle in out
    assert "(default)" in out  # the profile in use is marked


def test_help_works_without_a_proxy_config(script, env, tmp_path):
    r = _run(script, {**env, "JAILBEE_LITELLM_CONFIG": str(tmp_path / "absent.json")}, "-h")
    assert r.returncode == 0, r.stderr
    assert "--context" in r.stdout and "ARGV:-h" in r.stdout


def test_help_after_double_dash_is_not_ours(script, env):
    r = _run(script, env, "--", "--help")
    assert "--context" not in r.stdout
