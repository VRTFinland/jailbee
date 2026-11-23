from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import labels, watch
from jailbee.orchestration.actions import Node

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def supervised(**kwargs):
    return watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, **kwargs)


def advance(entry, status, *, seconds=0, child=None):
    from jailbee.orchestration.agent_watch import advance as step

    if child is None:
        child = Node("proj-kid", "kid", True, {labels.SPAWNED_BY: "proj-orch"})
    return step(entry, child, status, now=NOW + timedelta(seconds=seconds), has_jailbee=True)


def test_idle_episode_threshold_repeat_and_activity_reset() -> None:
    from jailbee.orchestration.agent_watch import advance_idle

    entry = supervised(confirmed=True)
    entry, body = advance_idle(entry, "session-turn-1", now=NOW, after=900)
    assert body is None
    entry, body = advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=899), after=900)
    assert body is None
    entry, body = advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=900), after=900)
    assert "15m" in body
    assert (
        advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=1799), after=900)[1]
        is None
    )
    entry, body = advance_idle(
        entry, "session-turn-1", now=NOW + timedelta(seconds=1800), after=900
    )
    assert "30m" in body
    entry, body = advance_idle(
        entry, "session-turn-2", now=NOW + timedelta(seconds=1801), after=900
    )
    assert body is None
    assert (
        advance_idle(entry, "session-turn-2", now=NOW + timedelta(seconds=2700), after=900)[1]
        is None
    )
    entry, body = advance_idle(entry, None, now=NOW + timedelta(seconds=2701), after=900)
    assert body is None and entry.idle_since is None and entry.idle_reported_at is None
    assert (
        advance_idle(entry, "session-turn-2", now=NOW + timedelta(seconds=5000), after=900)[1]
        is None
    )


def test_blocked_message_suppresses_idle() -> None:
    from jailbee.orchestration.agent_watch import advance_idle

    entry = supervised(confirmed=True, last_message_kind="blocked", idle_since=NOW.isoformat())
    assert advance_idle(entry, "turn", now=NOW + timedelta(hours=1), after=900)[1] is None


def test_alive_requires_two_separated_observations() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(True, "claude"))
    assert out is None and not e.confirmed
    e, out = advance(e, AgentStatus(True, "claude"), seconds=1)
    assert out is None
    e, out = advance(e, AgentStatus(True, "claude"), seconds=3)
    assert e.confirmed and out.state == "done" and "agent is alive" in out.detail
    assert advance(e, AgentStatus(True, "claude"), seconds=5)[1] is None


def test_readiness_fails_without_container_jailbee() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus
    from jailbee.orchestration.agent_watch import advance as step

    child = Node("proj-kid", "kid", True, {})
    entry = supervised(live_since=NOW.isoformat())
    _, outcome = step(
        entry, child, AgentStatus(True), now=NOW + timedelta(seconds=3), has_jailbee=False
    )
    assert outcome.state == "failed"
    assert "in-container jailbee" in outcome.detail


@pytest.mark.parametrize("rc", [0, 1, 127, 137, 143])
def test_early_exit_fails_and_late_exit_blocks(rc) -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(False, "claude", rc))
    assert e is None and out.state == "failed" and f"status {rc}" in out.detail
    e, out = advance(supervised(confirmed=True), AgentStatus(False, "claude", rc))
    assert e is None and out.state == "failed"  # Broker relays later failure as blocked.


def test_never_started_times_out_and_unknown_probe_retries() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(False))
    assert out is None
    assert advance(e, AgentStatus(False), seconds=121)[1].state == "failed"
    assert advance(supervised(), None, seconds=200) == (supervised(), None)


def test_done_suppresses_but_external_stop_reports() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    done = Node("proj-kid", "kid", True, {labels.STATE: labels.STATE_DONE})
    assert advance(supervised(confirmed=True), AgentStatus(False, rc=0), child=done) == (None, None)
    stopped = Node("proj-kid", "kid", False, {})
    assert advance(supervised(confirmed=True), None, child=stopped)[1].state == "failed"


@pytest.mark.parametrize("agent_only", [False, True])
def test_done_before_confirmation_still_ends_the_ref(agent_only) -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    done = Node("proj-kid", "kid", True, {labels.STATE: labels.STATE_DONE})
    entry = supervised(agent_only=agent_only)
    for status in (None, AgentStatus(False, rc=0)):
        updated, outcome = advance(entry, status, child=done)
        assert updated is None
        assert outcome is not None and outcome.state == "done"
        assert outcome.detail == "child `kid` is ready; it already reported done"


@pytest.mark.parametrize(
    "state,subagents,expected",
    [
        ("waiting", 0, True),
        ("waiting", 1, True),
        ("idle", 0, True),
        ("idle", 1, False),
        ("busy", 0, False),
        ("shell", 0, False),
        ("unknown", 0, False),
    ],
)
def test_adapter_idle_signal_matches_only_prompted_process(
    tmp_path, mocker, state, subagents, expected
):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    adapter = MagicMock()
    session = AgentSession("claude", 123, 42, state, "input needed", NOW, 1, "session")
    adapter.read_sessions.return_value = [session]
    adapter.read_activity.return_value = AgentActivity(
        None, "last", subagents=subagents, modified=10, background_clear=subagents == 0
    )
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    result = idle_signal(cfg, "kid", status, now=NOW)
    assert (result is not None) is expected
    assert (
        idle_signal(cfg, "kid", AgentStatus(True, "claude", pid=124, proc_start=42), now=NOW)
        is None
    )
    assert (
        idle_signal(cfg, "kid", AgentStatus(True, "claude", pid=123, proc_start=43), now=NOW)
        is None
    )


def test_old_background_work_cannot_be_treated_as_idle(tmp_path, mocker):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "idle", None, NOW, 1)]
    # A freshness count of zero is only an estimate, not completion evidence.
    adapter.read_activity.return_value = AgentActivity(None, "last", subagents=0)
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        idle_signal(
            make_config(tmp_path),
            "kid",
            AgentStatus(True, "claude", pid=123, proc_start=42),
            now=NOW,
        )
        is None
    )


def test_idle_signal_unknown_adapter_and_missing_transcript(tmp_path, mocker):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    lookup = mocker.patch(
        "jailbee.accounts.adapters.base.get_adapter", side_effect=KeyError("codex")
    )
    status = AgentStatus(True, "codex", pid=123, proc_start=42)
    assert idle_signal(cfg, "kid", status, now=NOW) is None
    adapter = MagicMock()
    lookup.side_effect = None
    lookup.return_value = adapter
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "idle", None, NOW, 1)]
    adapter.locate_activity.return_value = None
    assert idle_signal(cfg, "kid", status, now=NOW) is None


def test_probe_live_and_exited() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus, probe

    incus = MagicMock()
    incus.exec.return_value = json.dumps({"alive": True, "agent": "claude", "rc": None})
    assert probe(incus, "kid", uid=1000) == AgentStatus(True, "claude", None)
    assert incus.exec.call_args.kwargs == {"uid": 1000, "timeout": 15}
    incus.exec.return_value = json.dumps({"alive": False, "agent": "codex", "rc": 137})
    assert probe(incus, "kid", uid=1000) == AgentStatus(False, "codex", 137)


@pytest.mark.parametrize("value", ["", "no", "{}", '{"alive": "yes"}'])
def test_probe_bad_reply_is_unknown(value: str) -> None:
    from jailbee.orchestration.agent_watch import probe

    incus = MagicMock()
    incus.exec.return_value = value
    assert probe(incus, "kid", uid=1000) is None


def test_probe_error_is_retryable() -> None:
    from jailbee.orchestration.agent_watch import probe

    incus = MagicMock()
    incus.exec.side_effect = IncusError("temporarily unavailable")
    assert probe(incus, "kid", uid=1000) is None


@pytest.mark.parametrize(
    "rc,expected",
    [
        (0, "status 0"),
        (1, "status 1"),
        (127, "status 127"),
        (137, "SIGKILL"),
        (143, "SIGTERM"),
        (255, "signal 127"),
        (None, "unknown"),
    ],
)
def test_describe_exit(rc, expected) -> None:
    from jailbee.orchestration.agent_watch import AgentStatus, describe

    assert expected in describe(AgentStatus(False, "claude", rc))


@pytest.mark.parametrize(
    "state,start,boot,expected",
    [
        ("S", "42", "boot", True),
        ("Z", "42", "boot", False),
        ("S", "99", "boot", False),
        ("S", "42", "other", False),
    ],
)
def test_probe_script_checks_process_identity(
    monkeypatch, capsys, state, start, boot, expected
) -> None:
    from pathlib import Path

    from jailbee.orchestration.agent_watch import _PROBE

    files = {
        "runtime": json.dumps({"pid": 123, "start": "42", "boot": "boot", "agent": "claude"}),
        "/proc/123/stat": "123 (odd ) name) " + " ".join([state] + ["0"] * 18 + [start]),
        "/proc/sys/kernel/random/boot_id": boot,
    }

    def read(path, *args, **kwargs):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr("sys.argv", ["probe", "runtime", "exit"])
    exec(compile(_PROBE, "probe", "exec"), {})
    assert json.loads(capsys.readouterr().out)["alive"] is expected


CLAUDE_PREFIX = (
    "source /home/dev/.claude/shell-snapshots/snapshot-bash-1794338962781-3c64dq.sh"
    " 2>/dev/null || true && shopt -u extglob 2>/dev/null || true && "
    r"{ \builtin unalias -- 'which'; \builtin unset -f -- 'which'; }"
    " >/dev/null 2>&1 || true && "
    r"{ \builtin unalias -- 'unsetenv'; \builtin unset -f -- 'unsetenv'; }"
    " >/dev/null 2>&1 || true && "
)
CLAUDE_WAITER = "jailbee inbox wait --wake-on msg:done,question --quiet-after 60m --timeout none"
CLAUDE_SUFFIX = " && pwd -P >| /tmp/claude-d404-cwd"
CLAUDE_SCRIPT = CLAUDE_PREFIX + "eval '" + CLAUDE_WAITER + "'" + CLAUDE_SUFFIX


def claude_script(inner):
    return CLAUDE_PREFIX + "eval '" + inner.replace("'", "'\"'\"'") + "'" + CLAUDE_SUFFIX


CLAUDE_CASES = {
    "claude-follow": (CLAUDE_SCRIPT.replace("inbox wait", "inbox follow"), True),
    "claude-follow-queued": (
        claude_script("jailbee inbox follow; make deploy"),
        False,
    ),
    "claude-current-wrapper": (
        CLAUDE_SCRIPT.replace("shopt -u", r"\builtin shopt -u").replace(
            "'" + CLAUDE_SUFFIX, "' < /dev/null" + CLAUDE_SUFFIX
        ),
        True,
    ),
    "claude-semicolon": (claude_script(CLAUDE_WAITER + "; make deploy"), False),
    "claude-and": (claude_script(CLAUDE_WAITER + " && git push"), False),
    "claude-or": (claude_script(CLAUDE_WAITER + " || git push"), False),
    "claude-pipe": (claude_script(CLAUDE_WAITER + " | cat"), False),
    "claude-background": (claude_script(CLAUDE_WAITER + " &"), False),
    "claude-loop": (claude_script("while :; do jailbee inbox wait; done"), False),
    "claude-substitution": (claude_script(CLAUDE_WAITER + " $(make deploy)"), False),
    "claude-backtick": (claude_script(CLAUDE_WAITER + " `make deploy`"), False),
    "claude-inner-eval": (claude_script("eval '" + CLAUDE_WAITER + "'"), False),
    "claude-second-eval": (CLAUDE_SCRIPT + " && eval 'make deploy'", False),
    "claude-unknown-prefix": ("curl x | " + CLAUDE_SCRIPT, False),
    "claude-source-outside": (CLAUDE_SCRIPT.replace("/home/dev/.claude", "/tmp/.claude"), False),
    "claude-source-traversal": (
        CLAUDE_SCRIPT.replace("/snapshot-bash", "/../snapshot-bash"),
        False,
    ),
    "claude-trailing": (CLAUDE_SCRIPT + " && make deploy", False),
    "claude-broken-quote": (CLAUDE_PREFIX + "eval '" + CLAUDE_WAITER, False),
    "claude-double-quote": (CLAUDE_PREFIX + 'eval "' + CLAUDE_WAITER + '"', False),
    "claude-quote-injection": (
        CLAUDE_PREFIX + "eval '" + CLAUDE_WAITER + "'$(make deploy)''",
        False,
    ),
    "claude-newline": (claude_script(CLAUDE_WAITER + "\nmake deploy"), False),
    "claude-redirect": (claude_script(CLAUDE_WAITER + " > /tmp/file"), False),
    "claude-inner-cd": (claude_script("cd /tmp && " + CLAUDE_WAITER), False),
    "claude-python-cwd": ("cd /tmp/e && python3 -m jailbee inbox wait", False),
    "claude-python-inner-unsafe": (claude_script("python3 -m jailbee inbox wait"), False),
    "claude-python-inner-safe": (claude_script("python3 -P -m jailbee inbox wait"), True),
    "claude-escaped-quote": (claude_script("jailbee inbox wait --timeout 'none'"), True),
    "claude-quiet": (claude_script(CLAUDE_WAITER + " >/dev/null 2>&1"), True),
    "claude-stderr": (claude_script(CLAUDE_WAITER + " 2>/dev/null"), True),
    "claude-home-not-exported": (CLAUDE_SCRIPT, True),
    "claude-extra-argv": (CLAUDE_SCRIPT, False),
    "claude-wrong-option": (CLAUDE_SCRIPT, False),
    "claude-dash": (CLAUDE_SCRIPT, False),
    "claude-sh": (CLAUDE_SCRIPT, True),
    "claude-zsh": (CLAUDE_SCRIPT, False),
    "claude-no-prefix": ("eval '" + CLAUDE_WAITER + "'", True),
    "claude-no-suffix": (CLAUDE_SCRIPT.removesuffix(CLAUDE_SUFFIX), True),
    "claude-other-prefix": (
        "shopt -s nullglob && "
        r"{ \builtin unalias -- '_name2'; \builtin unset -f -- '_name2'; } && "
        "eval '" + CLAUDE_WAITER + "'",
        True,
    ),
    "claude-prefix-name": (CLAUDE_SCRIPT.replace("'which'", "'which; deploy'"), True),
    "claude-mismatched-name": (
        CLAUDE_SCRIPT.replace("unset -f -- 'which'", "unset -f -- 'other'"),
        True,
    ),
    "claude-prefix-statements": (CLAUDE_SCRIPT.replace("extglob", "extglob; make deploy"), False),
    "claude-other-cwd": (CLAUDE_SCRIPT.replace("d404-cwd", "nothex-cwd"), True),
    "claude-future-prefix": (r"\builtin set +o something || true && " + CLAUDE_SCRIPT, True),
    "claude-plain-prefix": ("custom_setup && " + CLAUDE_WAITER, False),
    "claude-external-prefix": ("make prep && " + CLAUDE_WAITER, False),
    "claude-other-source": ("source ./other.sh && " + CLAUDE_WAITER, False),
    "claude-trap-prefix": ("trap 'deploy' EXIT && " + CLAUDE_WAITER, False),
    "claude-exec-prefix": ("exec sleep 1 && " + CLAUDE_WAITER, False),
    "claude-subshell-prefix": ("( cd /tmp ) && " + CLAUDE_WAITER, False),
    "claude-hash-rebind": ("hash -p /tmp/hidden-helper jailbee; " + CLAUDE_SCRIPT, False),
    "claude-alias-rebind": ("alias jailbee=hidden_helper; " + CLAUDE_SCRIPT, False),
    "claude-export-path": ("export PATH=/tmp/e && " + CLAUDE_SCRIPT, False),
    **{
        f"claude-keyword-environment-rebind-{setup}": (
            "set -o posix; set -k; " + setup + "; python3 -P -m jailbee inbox wait",
            False,
        )
        for setup in (": PYTHONPATH=/tmp/e", "cd PATH=/tmp/e", "set PYTHONPATH=/tmp/e")
    },
    **{
        f"claude-environment-rebind-{prefix}-{setup}": (
            prefix + setup + " && " + CLAUDE_SCRIPT,
            False,
        )
        for prefix in ("", r"\builtin ", "command ")
        for setup in (
            "export PYTHONPATH=x",
            "declare -x LD_PRELOAD=x",
            "typeset -x PATH=/tmp/e",
            "readonly PATH=/tmp/e",
            "local PATH=/tmp/e",
            "export -f jailbee",
            "declare -fx jailbee",
            "declare -Fx jailbee",
            "typeset -n PATH=other",
            "export TEST=value",
            *(
                f"{builtin} -x {name}"
                for builtin in ("export", "declare", "typeset")
                for name in (
                    "PATH",
                    "LD_LIBRARY_PATH",
                    "PYTHONHOME",
                    "BASH_ENV",
                    "ENV",
                    "IFS",
                    "CDPATH",
                    "SHELLOPTS",
                    "BASHOPTS",
                    "PS4",
                    "PROMPT_COMMAND",
                )
            ),
        )
    },
    "claude-cwd-compact": ("eval '" + CLAUDE_WAITER + "'; pwd >/tmp/cwd", True),
    "claude-command-prefix": (r"\command shopt -s nullglob && " + CLAUDE_SCRIPT, True),
    "claude-dot-prefix": (
        ". '/home/dev/.claude/shell-snapshots/future-snapshot.sh' && " + CLAUDE_SCRIPT,
        True,
    ),
    "claude-prefix-redirect": ("set +o > /tmp/other && " + CLAUDE_SCRIPT, False),
    "claude-prefix-arithmetic": ("declare -i 'x=a[$(deploy)]' && " + CLAUDE_SCRIPT, False),
    "claude-group-external": ("{ make prep; }; " + CLAUDE_SCRIPT, False),
    "claude-group-trap": ("{ trap 'deploy' EXIT; }; " + CLAUDE_SCRIPT, False),
    "claude-group-source": ("{ source /tmp/x.sh; }; " + CLAUDE_SCRIPT, False),
    "claude-prefix-eval": ("eval 'true' && " + CLAUDE_SCRIPT, False),
    "claude-command-external": ("command make prep && " + CLAUDE_SCRIPT, False),
    "claude-builtin-empty": ("builtin && " + CLAUDE_SCRIPT, False),
    "claude-prefix-process-substitution": ("source <(make prep) && " + CLAUDE_SCRIPT, False),
    "claude-builtin-prefix": (
        r"\builtin set +o histexpand || true && export TEST && " + CLAUDE_SCRIPT,
        True,
    ),
    "claude-cwd-semicolon": ("eval '" + CLAUDE_WAITER + "'; pwd -P > /tmp/cwd", True),
    "claude-cwd-no-P": (CLAUDE_SCRIPT.replace("pwd -P", "pwd"), True),
    "claude-two-waiters": (CLAUDE_SCRIPT + " && jailbee inbox wait", False),
    "claude-trailing-semicolon": ("eval '" + CLAUDE_WAITER + "'; make deploy", False),
    "claude-trailing-and": ("eval '" + CLAUDE_WAITER + "' && git push", False),
    "claude-trailing-or-cwd": ("eval '" + CLAUDE_WAITER + "' || pwd > /tmp/cwd", False),
    "claude-trailing-or": ("eval '" + CLAUDE_WAITER + "' || x", False),
    "claude-trailing-pipe": ("eval '" + CLAUDE_WAITER + "' | tee /tmp/log", False),
    "claude-top-while": ("while :; do " + CLAUDE_WAITER + "; done", False),
    "claude-top-for": ("for x in a; do " + CLAUDE_WAITER + "; done", False),
    "claude-top-if": ("if true; then " + CLAUDE_WAITER + "; fi", False),
    "claude-top-brace": ("{ " + CLAUDE_WAITER + "; }", False),
    "claude-top-subshell": ("(" + CLAUDE_WAITER + ")", False),
    "claude-top-function": ("f() { " + CLAUDE_WAITER + "; }; f", False),
    "claude-prefix-compound-waiter": ("{ " + CLAUDE_WAITER + "; }; " + CLAUDE_SCRIPT, False),
    "claude-prefix-quoted-waiter": ("{ 'jailbee' 'inbox' 'wait'; }; " + CLAUDE_SCRIPT, False),
    "claude-prefix-eval-waiter": (
        "eval 'jailbee inbox wait; make deploy' && " + CLAUDE_SCRIPT,
        False,
    ),
    "claude-prefix-background": ("sleep 1 & " + CLAUDE_SCRIPT, False),
    "claude-builtin-background": ("true & " + CLAUDE_SCRIPT, False),
    "claude-prefix-heredoc": ("cat << EOF && " + CLAUDE_SCRIPT, False),
    "claude-prefix-substitution": ("x=$(echo '" + CLAUDE_WAITER + "') && " + CLAUDE_SCRIPT, False),
    "claude-prefix-unbalanced": ("{ true && " + CLAUDE_SCRIPT, False),
    "claude-cwd-extra": (CLAUDE_SCRIPT + " > /tmp/other", False),
    "claude-eval-input": ("eval '" + CLAUDE_WAITER + "' < /dev/null", True),
    "claude-input-other": ("eval '" + CLAUDE_WAITER + "' < /tmp/input", False),
    "claude-revalidated": (CLAUDE_SCRIPT, None),
}


@pytest.mark.parametrize(
    "command,extra,expected",
    [
        (
            "python -P -m jailbee.container_main inbox wait --wake-on message --timeout none",
            None,
            True,
        ),
        ("/opt/venv/bin/python -P -m jailbee.container_main wait --timeout none", None, True),
        ("jailbee inbox wait --timeout none", None, True),
        ("jailbee inbox follow", None, True),
        ("python -P -m jailbee.container_main inbox follow", None, True),
        ("python -m jailbee.container_main inbox follow", None, False),
        (
            "/opt/jailbee/venv/bin/python -P -m jailbee.container_main inbox wait --timeout 20s",
            "claude-wrapper",
            True,
        ),
        ("jailbee inbox wait", "no-children", False),
        ("jailbee inbox wait", "make-parent", False),
        ("jailbee inbox wait", "python-parent", False),
        ("jailbee inbox send message --wait", None, False),
        ("jailbee service logs x --wait ready", None, False),
        ("python -m unrelated inbox wait", None, False),
        *[
            (f"{python} -m {module} inbox wait", None, False)
            for python in ("python", "python3", "/opt/venv/bin/python3.13")
            for module in ("jailbee", "jailbee.container_main", "jailbee.inbox")
        ],
        ("python3 -m jailbee inbox wait", "claude-python-cwd", False),
        ("sleep 600", None, False),
        ("pytest", None, False),
        ("jailbee inbox wait", "sibling", False),
        ("jailbee inbox wait", "descendant", False),
        ("jailbee inbox wait", "unreadable", None),
        ("jailbee inbox wait", "missing", None),
        ("jailbee inbox wait", "changed", None),
        ("jailbee inbox wait", "new-descendant", None),
        ("jailbee inbox wait", "new-sibling", None),
        ("jailbee inbox wait", "exec-changed", None),
        ("jailbee inbox wait", "queued-semicolon", False),
        ("jailbee inbox wait", "queued-and", False),
        ("jailbee inbox wait", "queued-loop", False),
        ("jailbee inbox wait", "eval-wrapper", False),
        ("jailbee inbox wait", "shell-unreadable", None),
        ("jailbee inbox wait", "shell-exec-changed", None),
        *[("jailbee inbox wait", label, expected) for label, (_, expected) in CLAUDE_CASES.items()],
    ],
)
def test_probe_checks_real_waiter_command_and_descendants(
    monkeypatch, capsys, command, extra, expected
):
    from pathlib import Path

    from jailbee.orchestration.agent_watch import probe

    monkeypatch.setattr("os.getuid", lambda: 1000)
    monkeypatch.setattr("pwd.getpwuid", lambda uid: MagicMock(pw_dir="/home/dev"))

    def stat(pid, parent):
        parent_name = {"make-parent": "make", "python-parent": "python"}.get(extra, "bash")
        comm = parent_name if pid == 124 else "process"
        return f"{pid} ({comm}) " + " ".join(["S", str(parent)] + ["0"] * 17 + ["42"])

    files = {
        "runtime": json.dumps({"pid": 123, "start": "42", "boot": "boot", "agent": "claude"}),
        "/proc/123/stat": stat(123, 1),
        "/proc/124/stat": stat(124, 123),
        "/proc/125/stat": stat(125, 124),
        "/proc/124/cmdline": b"bash\0-c\0jailbee inbox wait --timeout none\0",
        "/proc/125/cmdline": ("\0".join(command.split()) + "\0").encode(),
        "/proc/sys/kernel/random/boot_id": "boot",
    }

    scripts = {
        "claude-wrapper": CLAUDE_SCRIPT,
        **{label: script for label, (script, _) in CLAUDE_CASES.items()},
        "queued-semicolon": "jailbee inbox wait --timeout none; make deploy",
        "queued-and": "jailbee inbox wait && git push",
        "queued-loop": "while :; do jailbee inbox wait; ./handle.sh; done",
        "eval-wrapper": "source snapshot.sh || true && eval 'jailbee inbox wait' && pwd -P > cwd",
    }
    if extra in scripts:
        shell = {"claude-dash": "dash", "claude-sh": "/bin/sh", "claude-zsh": "zsh"}.get(
            extra, "/bin/bash"
        )
        option = "-x" if extra == "claude-wrong-option" else "-c"
        argv = [shell, option, scripts[extra]]
        if extra == "claude-extra-argv":
            argv.append("ignored")
        files["/proc/124/cmdline"] = ("\0".join(argv) + "\0").encode()
    shell_reads = 0
    pids = [123] if extra == "no-children" else [123, 124, 125]
    if extra in ("sibling", "descendant"):
        pids.append(126)
        files["/proc/126/stat"] = stat(126, 124 if extra == "sibling" else 125)
        files["/proc/126/cmdline"] = b"sleep\0" + b"600\0"
    reads = 0
    cmd_reads = 0

    def read(path, *args, **kwargs):
        nonlocal reads, cmd_reads, shell_reads
        key = str(path)
        if key == "/proc/124/cmdline":
            shell_reads += 1
            if extra == "shell-unreadable":
                raise PermissionError(key)
            if extra in ("shell-exec-changed", "claude-revalidated") and shell_reads > 1:
                return b"bash\0-c\0jailbee inbox wait; make deploy\0"
        if key == "/proc/125/cmdline":
            cmd_reads += 1
            if extra == "exec-changed" and cmd_reads > 1:
                return b"sleep\0" + b"600\0"
            if extra in ("new-descendant", "new-sibling") and 126 not in pids:
                pids.append(126)
                files["/proc/126/stat"] = stat(126, 125 if extra == "new-descendant" else 123)
                files["/proc/126/cmdline"] = b"sleep\0" + b"600\0"
            if extra == "unreadable":
                raise PermissionError(key)
            if extra == "missing":
                raise FileNotFoundError(key)
        if key == "/proc/125/stat":
            reads += 1
            if extra == "changed" and reads > 1:
                return stat(125, 999)
        try:
            return files[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(Path(f"/proc/{pid}") for pid in pids))
    incus = MagicMock()

    def execute(name, command, **kwargs):
        monkeypatch.setattr("sys.argv", ["probe", "runtime", "exit", *command[5:]])
        exec(compile(command[2], "probe", "exec"), {})
        return capsys.readouterr().out

    incus.exec.side_effect = execute
    status = probe(incus, "kid", uid=1000, background=True)
    assert status is not None and status.alive
    assert status.waiters_only is expected
    if expected is not None:
        assert status.processes_clear is (extra == "no-children")
    if expected is True or extra == "no-children":
        assert isinstance(status.process_snapshot, str) and status.process_snapshot


def test_output_tail_is_bounded_and_best_effort() -> None:
    from jailbee.orchestration.agent_watch import output_tail

    incus = MagicMock()
    incus.exec.return_value = "x" * 10000
    tail = output_tail(incus, "kid", "claude", uid=1000)
    assert len(tail) <= 4096
    assert "capture-pane" in " ".join(incus.exec.call_args.args[1])
    incus.exec.side_effect = IncusError("no pane")
    assert output_tail(incus, "kid", "claude", uid=1000) == ""


def test_current_session_matches_prompted_pid(tmp_path, mocker):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    a = "00000000-0000-0000-0000-000000000001"
    b = "00000000-0000-0000-0000-000000000002"
    adapter.read_sessions.return_value = [
        AgentSession("claude", 124, 42, "waiting", None, NOW, 1, a),
        AgentSession("claude", 123, 42, "waiting", None, NOW, 1, b),
    ]
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        == b
    )
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=43)
        )
        is None
    )


@pytest.mark.parametrize("session_id", ["../x", "$(id)", "AAAAAAAA-0000-0000-0000-000000000000"])
def test_current_session_rejects_non_uuid(tmp_path, mocker, session_id):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.return_value = [
        AgentSession("claude", 123, 42, "waiting", None, NOW, 1, session_id)
    ]
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        is None
    )


def test_current_session_reader_raises_returns_none(tmp_path, mocker):
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.side_effect = RuntimeError("broken")
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        is None
    )


def test_agent_snapshot_busy_session_has_last_event(tmp_path, mocker):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, agent_snapshot
    from tests.conftest import make_config

    event = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    adapter = MagicMock()
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "busy", None, NOW, 1)]
    adapter.read_activity.return_value = AgentActivity(
        "Bash\x1b[0m", "msg", subagents=0, last_event_at=event
    )
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    snap = agent_snapshot(make_config(tmp_path), "kid", status, now=NOW)
    assert snap == {
        "alive": True,
        "state": "busy",
        "session_state": "busy",
        "since": NOW.timestamp(),
        "last_event_at": event.timestamp(),
        "last_tool": "Bash?[0m",
        "error": None,
    }


def test_agent_snapshot_reports_an_open_api_error_as_the_state(tmp_path, mocker):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, agent_snapshot
    from tests.conftest import make_config

    resets = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
    error = _api_error()
    adapter = MagicMock()
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "idle", None, NOW, 1)]
    adapter.read_activity.return_value = AgentActivity(
        None,
        "API Error: raw",
        api_error=error.__class__("quota", NOW, 429, "limit \x1b[0m reached", resets, "k"),
    )
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    snap = agent_snapshot(make_config(tmp_path), "kid", status, now=NOW)
    assert snap["state"] == "error:quota"
    assert snap["session_state"] == "idle"
    assert snap["error"] == {
        "kind": "quota",
        "at": NOW.timestamp(),
        "detail": "limit ?[0m reached",
        "resets_at": resets.timestamp(),
    }


def test_agent_snapshot_adapter_error_keeps_alive(tmp_path, mocker):
    from jailbee.orchestration.agent_watch import AgentStatus, agent_snapshot
    from tests.conftest import make_config

    mocker.patch("jailbee.accounts.adapters.base.get_adapter", side_effect=RuntimeError("boom"))
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    assert agent_snapshot(make_config(tmp_path), "kid", status, now=NOW) == {
        "alive": True,
        "state": None,
        "session_state": None,
        "since": None,
        "last_event_at": None,
        "last_tool": None,
        "error": None,
    }


def test_agent_snapshot_never_recorded_agent_is_starting(tmp_path):
    from jailbee.orchestration.agent_watch import AgentStatus, agent_snapshot
    from tests.conftest import make_config

    snap = agent_snapshot(make_config(tmp_path), "kid", AgentStatus(False), now=NOW)
    assert snap["alive"] is None
    assert snap["state"] == "starting"


@pytest.mark.parametrize(
    "status_kwargs", [{"recorded": True}, {"recorded": True, "rc": 1}, {"rc": 0}]
)
def test_agent_snapshot_recorded_or_exited_agent_is_dead(tmp_path, status_kwargs):
    from jailbee.orchestration.agent_watch import AgentStatus, agent_snapshot
    from tests.conftest import make_config

    snap = agent_snapshot(
        make_config(tmp_path), "kid", AgentStatus(False, **status_kwargs), now=NOW
    )
    assert snap["alive"] is False
    assert snap["state"] is None


def _api_error(key="2026-10-30T00:00:00+00:00|e1", kind="quota"):
    from jailbee.accounts.models import AgentApiError

    return AgentApiError(
        kind=kind,
        at=NOW,
        status=429,
        detail="The usage limit has been reached",
        resets_at=datetime(2026, 11, 2, 12, 30, tzinfo=UTC),
        key=key,
    )


def test_api_error_reports_once_per_episode_and_clears_on_progress() -> None:
    from jailbee.orchestration.agent_watch import advance_api_error

    entry = supervised(confirmed=True)
    entry, body = advance_api_error(entry, _api_error())
    assert body == (
        "[jailbee] child `proj-kid` agent stopped on an API error (quota): "
        "The usage limit has been reached; resets at 2026-11-02 12:30 UTC"
    )
    assert entry.api_error_key == "2026-10-30T00:00:00+00:00|e1"
    entry, body = advance_api_error(entry, _api_error())
    assert body is None
    entry, body = advance_api_error(entry, None)
    assert body is None and entry.api_error_key is None
    entry, body = advance_api_error(entry, _api_error("later|e2", kind="overloaded"))
    assert body is not None and "(overloaded)" in body and "resets at" in body


def test_api_error_episode_survives_a_watch_reload(tmp_path) -> None:
    from jailbee.orchestration.agent_watch import advance_api_error

    entry, body = advance_api_error(supervised(confirmed=True), _api_error())
    assert body is not None
    watch.save_watch(tmp_path, {"proj-orch": [entry]})
    (reloaded,) = watch.load_watch(tmp_path)["proj-orch"]
    assert advance_api_error(reloaded, _api_error())[1] is None


def test_api_error_without_reset_or_detail() -> None:
    from dataclasses import replace

    from jailbee.orchestration.agent_watch import advance_api_error

    error = replace(_api_error(kind="auth"), resets_at=None, detail="")
    body = advance_api_error(supervised(confirmed=True), error)[1]
    assert body == "[jailbee] child `proj-kid` agent stopped on an API error (auth)"


def test_open_api_error_suppresses_idle() -> None:
    from jailbee.orchestration.agent_watch import advance_idle

    entry = supervised(confirmed=True, api_error_key="k", idle_since=NOW.isoformat())
    assert advance_idle(entry, "turn", now=NOW + timedelta(hours=1), after=900)[1] is None


def test_an_unreadable_transcript_keeps_the_episode_open() -> None:
    from jailbee.orchestration.agent_watch import UNKNOWN, advance_api_error

    entry, body = advance_api_error(supervised(confirmed=True), _api_error())
    assert body is not None
    entry, body = advance_api_error(entry, UNKNOWN)
    assert body is None and entry.api_error_key is not None
    assert advance_api_error(entry, _api_error())[1] is None


def test_api_error_read_failure_is_unknown_not_progress(tmp_path, mocker):
    from jailbee.orchestration.agent_watch import UNKNOWN, AgentStatus, api_error
    from tests.conftest import make_config

    mocker.patch("jailbee.accounts.adapters.base.get_adapter", side_effect=RuntimeError("boom"))
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    assert api_error(make_config(tmp_path), "kid", status, now=NOW) is UNKNOWN
