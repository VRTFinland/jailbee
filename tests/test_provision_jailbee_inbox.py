"""Runs the `jailbee-inbox` script install.sh writes against a temporary
orchestration directory. Needs bash and jq (both in the golden image)."""

import importlib.resources
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

_MARKER = "cat > /usr/local/bin/jailbee-inbox <<'EOF'\n"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

FIRST = "20261025T100000Z-00000001-h-0000aaaa.json"
SECOND = "20261025T100001Z-00000002-h-0000bbbb.json"


def install_sh_text() -> str:
    return importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()


@pytest.fixture
def script(tmp_path: Path) -> Path:
    text = install_sh_text()
    assert _MARKER in text
    start = text.index(_MARKER) + len(_MARKER)
    body = text[start : text.index("\nEOF\n", start)]
    path = tmp_path / "jailbee-inbox"
    path.write_text(body + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    d = tmp_path / "orch"
    (d / "inbox").mkdir(parents=True)
    return d


def message(mid: str, body: str = "hello there") -> dict[str, object]:
    return {
        "version": 1,
        "id": mid,
        "type": "message",
        "from": "orchestrator",
        "kind": "info",
        "body": body,
    }


def status(mid: str, ref: str = "m-1") -> dict[str, object]:
    return {
        "version": 1,
        "id": mid,
        "type": "status",
        "ref": ref,
        "state": "done",
        "detail": "all good",
    }


def write_inbox(base: Path, name: str, payload: dict[str, object]) -> Path:
    path = base / "inbox" / name
    path.write_text(json.dumps(payload))
    return path


def env_for(base: Path, tmp_path: Path, path: str | None = None) -> dict[str, str]:
    bin_dir = tmp_path / "nobin"
    bin_dir.mkdir(exist_ok=True)
    for tool in (
        "jq",
        "sleep",
        "ls",
        "mv",
        "mkdir",
        "date",
        "cat",
        "head",
        "sort",
        "env",
        "bash",
        "od",
        "tr",
        "wc",
        "rm",
    ):
        found = shutil.which(tool)
        assert found
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(found)
    return {
        "PATH": path if path is not None else str(bin_dir),
        "HOME": str(tmp_path),
        "JAILBEE_ORCHESTRATION_DIR": str(base),
        "JAILBEE_INBOX_POLL": "0.1",
    }


def run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, timeout=30
    )


def test_wait_returns_immediately_when_message_waiting(script, base, tmp_path):
    write_inbox(base, FIRST, message("h-0000aaaa"))
    start = time.monotonic()
    r = run(script, "wait", "--timeout", "5", env=env_for(base, tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith(FIRST)
    assert time.monotonic() - start < 2


def test_wait_prints_oldest_first(script, base, tmp_path):
    write_inbox(base, SECOND, message("h-0000bbbb"))
    write_inbox(base, FIRST, message("h-0000aaaa"))
    r = run(script, "wait", "--timeout", "0", env=env_for(base, tmp_path))
    assert r.returncode == 0
    assert r.stdout.strip().endswith(FIRST)


def test_wait_sees_new_message_via_poll_fallback(script, base, tmp_path):
    env = env_for(base, tmp_path)

    def later() -> None:
        time.sleep(0.3)
        write_inbox(base, FIRST, message("h-0000aaaa"))

    t = threading.Thread(target=later)
    t.start()
    r = run(script, "wait", "--timeout", "10", env=env)
    t.join()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith(FIRST)


def test_wait_times_out_with_exit_1(script, base, tmp_path):
    start = time.monotonic()
    r = run(script, "wait", "--timeout", "1", env=env_for(base, tmp_path))
    assert r.returncode == 1
    assert r.stdout == ""
    assert 0.8 < time.monotonic() - start < 5


def test_wait_timeout_zero_checks_once(script, base, tmp_path):
    assert run(script, "wait", "--timeout", "0", env=env_for(base, tmp_path)).returncode == 1


def test_wait_accepts_duration_suffixes(script, base, tmp_path):
    write_inbox(base, FIRST, message("h-0000aaaa"))
    for dur in ("5", "5s", "1m", "1h"):
        r = run(script, "wait", "--timeout", dur, env=env_for(base, tmp_path))
        assert r.returncode == 0, dur


@pytest.mark.parametrize("dur", ["abc", "-1", "5d", "", "1.5"])
def test_wait_rejects_bad_timeout(script, base, tmp_path, dur):
    r = run(script, "wait", "--timeout", dur, env=env_for(base, tmp_path))
    assert r.returncode == 2
    assert r.stderr.startswith("jailbee-inbox: ")


def test_wait_rejects_bad_poll_and_usage(script, base, tmp_path):
    env = env_for(base, tmp_path)
    env["JAILBEE_INBOX_POLL"] = "0"
    assert run(script, "wait", "--timeout", "0", env=env).returncode == 2
    env["JAILBEE_INBOX_POLL"] = "x"
    assert run(script, "wait", "--timeout", "0", env=env).returncode == 2
    env = env_for(base, tmp_path)
    assert run(script, env=env).returncode == 2
    assert run(script, "bogus", env=env).returncode == 2
    assert run(script, "wait", "--bogus", env=env).returncode == 2
    assert run(script, "read", env=env).returncode == 2
    assert run(script, "--help", env=env).returncode == 0


def test_wait_ignores_temp_read_dotfiles_and_symlinks(script, base, tmp_path):
    (base / "inbox" / ".x.json.tmp").write_text("{}")
    (base / "inbox" / ".hidden.json").write_text("{}")
    (base / "inbox" / "read").mkdir()
    (base / "inbox" / "read" / "old.json").write_text("{}")
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (base / "inbox" / "link.json").symlink_to(target)
    r = run(script, "wait", "--timeout", "1", env=env_for(base, tmp_path))
    assert r.returncode == 1


def test_wait_with_missing_inbox_dir_times_out(script, tmp_path):
    base = tmp_path / "absent"
    assert run(script, "wait", "--timeout", "0", env=env_for(base, tmp_path)).returncode == 1


def test_wait_uses_inotifywait_when_present(script, base, tmp_path):
    log = tmp_path / "inotify.log"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "inotifywait"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f'echo "{{}}" > "{base}/inbox/.probe.tmp"\n'
        f"echo '{json.dumps(message('h-0000aaaa'))}' > \"{base}/inbox/{FIRST}\"\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    env = env_for(base, tmp_path)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    r = run(script, "wait", "--timeout", "10", env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith(FIRST)
    args = log.read_text().split()
    assert args[:2] == ["-q", "-t"]
    assert 1 <= int(args[2]) <= 10
    assert args[3:] == ["-e", "close_write,moved_to", str(base / "inbox")]


def test_wait_rechecks_after_inotifywait_returns_empty(script, base, tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    counter = tmp_path / "count"
    stub = stubs / "inotifywait"
    stub.write_text(
        "#!/bin/bash\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}\n"
        'if [ "$n" -ge 1 ]; then\n'
        f"  echo '{json.dumps(message('h-0000aaaa'))}' > \"{base}/inbox/{FIRST}\"\n"
        "fi\n"
        "sleep 0.2\nexit 2\n"
    )
    stub.chmod(0o755)
    env = env_for(base, tmp_path)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    r = run(script, "wait", "--timeout", "10", env=env)
    assert r.returncode == 0, r.stderr
    assert int(counter.read_text()) == 2


def test_ls_empty_prints_nothing(script, base, tmp_path):
    r = run(script, "ls", env=env_for(base, tmp_path))
    assert (r.returncode, r.stdout) == (0, "")


def test_ls_lists_unread_oldest_first(script, base, tmp_path):
    write_inbox(base, SECOND, status("h-0000bbbb", ref="m-9"))
    write_inbox(base, FIRST, message("h-0000aaaa", body="x" * 100))
    (base / "inbox" / "read").mkdir()
    (base / "inbox" / "read" / "old.json").write_text(json.dumps(message("old")))
    r = run(script, "ls", env=env_for(base, tmp_path))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines == [
        "h-0000aaaa\tmessage\torchestrator\tinfo\t" + "x" * 60,
        "h-0000bbbb\tstatus\tm-9\tdone\tall good",
    ]


def test_ls_skips_corrupt_files_with_warning(script, base, tmp_path):
    (base / "inbox" / "0000-00000000-bad.json").write_text("not json{")
    write_inbox(base, FIRST, message("h-0000aaaa"))
    r = run(script, "ls", env=env_for(base, tmp_path))
    assert r.returncode == 0
    assert r.stdout.startswith("h-0000aaaa\t")
    assert len(r.stdout.splitlines()) == 1
    assert "0000-00000000-bad.json" in r.stderr


def test_read_prints_and_moves_to_read(script, base, tmp_path):
    path = write_inbox(base, FIRST, message("h-0000aaaa"))
    r = run(script, "read", "h-0000aaaa", env=env_for(base, tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["id"] == "h-0000aaaa"
    assert "\n  " in r.stdout  # pretty-printed
    assert not path.exists()
    assert (base / "inbox" / "read" / FIRST).exists()


def test_read_keep_leaves_file(script, base, tmp_path):
    path = write_inbox(base, FIRST, message("h-0000aaaa"))
    r = run(script, "read", "h-0000aaaa", "--keep", env=env_for(base, tmp_path))
    assert r.returncode == 0
    assert path.exists()
    assert not (base / "inbox" / "read" / FIRST).exists()


def test_read_unknown_id_exits_1(script, base, tmp_path):
    write_inbox(base, FIRST, message("h-0000aaaa"))
    assert run(script, "read", "nope", env=env_for(base, tmp_path)).returncode == 1


def test_read_ignores_corrupt_and_already_read(script, base, tmp_path):
    (base / "inbox" / "0000-00000000-bad.json").write_text("garbage")
    (base / "inbox" / "read").mkdir()
    (base / "inbox" / "read" / "old.json").write_text(json.dumps(message("h-0000cccc")))
    write_inbox(base, FIRST, message("h-0000aaaa"))
    env = env_for(base, tmp_path)
    assert run(script, "read", "h-0000aaaa", env=env).returncode == 0
    assert run(script, "read", "h-0000cccc", env=env).returncode == 1


def test_install_sh_installs_inotify_tools():
    assert "inotify-tools" in install_sh_text()


def test_script_is_chmodded_in_install_sh():
    assert "chmod 0755 /usr/local/bin/jailbee-inbox" in install_sh_text()


def _stub(tmp_path: Path, body: str) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    stub = stubs / "inotifywait"
    stub.write_text("#!/bin/bash\n" + body)
    stub.chmod(0o755)
    return stubs


def test_inotify_watch_is_capped_to_a_short_slice(script, base, tmp_path):
    log = tmp_path / "t.log"
    stubs = _stub(
        tmp_path,
        f'printf "%s\\n" "$3" >> {log}\n'
        f"echo '{json.dumps(message('h-0000aaaa'))}' > \"{base}/inbox/{FIRST}\"\n"
        "exit 0\n",
    )
    env = env_for(base, tmp_path)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    r = run(script, "wait", "--timeout", "600", env=env)
    assert r.returncode == 0, r.stderr
    assert int(log.read_text().split()[0]) <= 10


def test_wait_finds_message_that_landed_without_an_event(script, base, tmp_path):
    counter = tmp_path / "count"
    stubs = _stub(
        tmp_path,
        f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}\n"
        'if [ "$n" -ge 1 ]; then exit 2; fi\n'
        "sleep 0.3\nexit 2\n",
    )
    env = env_for(base, tmp_path)
    env["PATH"] = f"{stubs}:{env['PATH']}"

    def later() -> None:
        while not counter.exists():
            time.sleep(0.02)
        write_inbox(base, FIRST, message("h-0000aaaa"))  # no event for the stub

    t = threading.Thread(target=later)
    t.start()
    r = run(script, "wait", "--timeout", "30", env=env)
    t.join()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith(FIRST)


def test_read_of_a_file_that_vanishes_exits_1(script, base, tmp_path):
    path = write_inbox(base, FIRST, message("h-0000aaaa"))
    env = env_for(base, tmp_path)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    real_jq = shutil.which("jq")
    shim = shim_dir / "jq"
    shim.write_text(
        f'#!/bin/bash\nif [ "$1" = "." ]; then rm -f "{path}"; exit 2; fi\nexec {real_jq} "$@"\n'
    )
    shim.chmod(0o755)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    r = run(script, "read", "h-0000aaaa", env=env)
    assert r.returncode == 1
    assert "no unread message with id h-0000aaaa" in r.stderr


# --- send ---------------------------------------------------------------

SEND_ID_RE = r"^[a-z0-9][a-z0-9-]{0,63}$"


def send(
    script: Path,
    base: Path,
    tmp_path: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), "send", *args],
        env=env or env_for(base, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        input=stdin,
    )


def manifest_of(base: Path, r: subprocess.CompletedProcess[str]) -> tuple[str, dict[str, object]]:
    assert r.returncode == 0, r.stderr
    mid = r.stdout.strip()
    assert r.stdout == mid + "\n"
    return mid, json.loads((base / "outbox" / f"{mid}.json").read_text())


def test_send_message_writes_manifest_and_body(script, base, tmp_path):
    r = send(
        script,
        base,
        tmp_path,
        "message",
        "--kind",
        "question",
        "--reply-to",
        "h-0000aaaa",
        "--text",
        "which db?",
    )
    mid, m = manifest_of(base, r)
    assert re.match(SEND_ID_RE, mid) and re.match(r"^message-\d{14}-[0-9a-f]{4}$", mid)
    assert m == {
        "version": 1,
        "id": mid,
        "type": "message",
        "kind": "question",
        "in_reply_to": "h-0000aaaa",
        "body_file": f"{mid}.md",
    }
    assert (base / "outbox" / f"{mid}.md").read_text() == "which db?"


def test_send_message_minimal_and_to(script, base, tmp_path):
    mid, m = manifest_of(
        base, send(script, base, tmp_path, "message", "--to", "c1", "--text", "hi")
    )
    assert m == {"version": 1, "id": mid, "type": "message", "to": "c1", "body_file": f"{mid}.md"}


def test_send_spawn_fork_with_rights(script, base, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do it\n")
    r = send(
        script,
        base,
        tmp_path,
        "spawn",
        "--branch",
        "fix-1",
        "--mount",
        "data",
        "--egress",
        "pypi.org",
        "--file",
        str(prompt),
    )
    mid, m = manifest_of(base, r)
    assert m["kind"] == "fork"
    assert m["rights"] == {"version": 1, "mounts": ["data"], "loose": False, "egress": ["pypi.org"]}
    assert m["prompt_file"] == f"{mid}.md" and "base" not in m and "note" not in m
    assert (base / "outbox" / f"{mid}.md").read_text() == "do it\n"


def test_send_spawn_new_with_base_and_repeats(script, base, tmp_path):
    r = send(
        script,
        base,
        tmp_path,
        "spawn",
        "--branch",
        "feat/x",
        "--new",
        "--base",
        "main",
        "--mount",
        "a",
        "--mount",
        "b",
        "--egress",
        "x.io",
        "--egress",
        "y.io",
        "--loose",
        "--note",
        "why",
        "--text",
        "p",
    )
    _, m = manifest_of(base, r)
    assert m["kind"] == "new" and m["base"] == "main" and m["note"] == "why"
    assert m["rights"] == {
        "version": 1,
        "mounts": ["a", "b"],
        "loose": True,
        "egress": ["x.io", "y.io"],
    }


def test_send_spawn_empty_rights_written_in_full(script, base, tmp_path):
    _, m = manifest_of(base, send(script, base, tmp_path, "spawn", "--branch", "b", "--text", "p"))
    assert m["rights"] == {"version": 1, "mounts": [], "loose": False, "egress": []}


def test_send_destroy_force(script, base, tmp_path):
    mid, m = manifest_of(
        base, send(script, base, tmp_path, "destroy", "--child", "fix-1", "--force")
    )
    assert m == {"version": 1, "id": mid, "type": "destroy", "child": "fix-1", "force": True}
    mid, m = manifest_of(base, send(script, base, tmp_path, "destroy", "--child", "fix-1"))
    assert "force" not in m


def test_send_restart_import_stop(script, base, tmp_path):
    mid, m = manifest_of(
        base, send(script, base, tmp_path, "restart", "--child", "c", "--text", "again")
    )
    assert m == {
        "version": 1,
        "id": mid,
        "type": "restart",
        "child": "c",
        "prompt_file": f"{mid}.md",
    }
    for t in ("import", "stop"):
        mid, m = manifest_of(base, send(script, base, tmp_path, t, "--child", "c"))
        assert m == {"version": 1, "id": mid, "type": t, "child": "c"}
        assert not (base / "outbox" / f"{mid}.md").exists()


def test_send_reads_stdin_dash(script, base, tmp_path):
    r = send(script, base, tmp_path, "message", "-", stdin="from stdin\nline 2\n")
    mid, _ = manifest_of(base, r)
    assert (base / "outbox" / f"{mid}.md").read_text() == "from stdin\nline 2\n"


def test_send_creates_outbox_and_leaves_no_temp_file(script, tmp_path):
    base = tmp_path / "fresh"
    r = send(script, base, tmp_path, "message", "--text", "x")
    assert r.returncode == 0
    names = os.listdir(base / "outbox")
    assert not [n for n in names if n.startswith(".")]
    assert len(names) == 2


@pytest.mark.parametrize(
    "args",
    [
        ["bogus"],
        [],
        ["spawn", "--text", "p"],
        ["spawn", "--branch", "b"],
        ["spawn", "--branch", "b", "--text", "p", "--bogus"],
        ["spawn", "--branch", "b", "--base", "main", "--text", "p"],
        ["message", "--kind", "weird", "--text", "p"],
        ["message", "--text", "a", "--file", "x"],
        ["message"],
        ["message", "--text", ""],
        ["message", "--text", "p", "--branch", "b"],
        ["import"],
        ["stop", "--child", "c", "--text", "p"],
        ["restart", "--child", "c"],
        ["import", "--child", "c", "--force"],
        ["message", "--text", "p", "--child", "c"],
        ["message", "--to"],
        [
            "message",
            "--file",
            "/nonexistent",
        ],
    ],
)
def test_send_bad_usage_exits_2_and_writes_nothing(script, base, tmp_path, args):
    r = send(script, base, tmp_path, *args)
    assert r.returncode == 2, r.stderr
    assert r.stderr.startswith("jailbee-inbox: ")
    assert r.stdout == ""
    assert not list((base / "outbox").glob("*")) if (base / "outbox").exists() else True


def test_send_refuses_oversized_text(script, base, tmp_path):
    ok = send(script, base, tmp_path, "message", "--text", "x" * 65536)
    assert ok.returncode == 0
    r = send(script, base, tmp_path, "message", "-", stdin="x" * 65537)
    assert r.returncode == 2 and "65536" in r.stderr
    assert len(os.listdir(base / "outbox")) == 2  # only the accepted one


def test_send_refuses_to_overwrite_existing_id(script, base, tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "od").write_text("#!/bin/bash\necho ' ab cd'\n")
    (shim / "date").write_text("#!/bin/bash\necho 20261025120000\n")
    for f in shim.iterdir():
        f.chmod(0o755)
    env = env_for(base, tmp_path)
    env["PATH"] = f"{shim}:{env['PATH']}"
    first = send(script, base, tmp_path, "stop", "--child", "c", env=env)
    assert first.returncode == 0 and first.stdout.strip() == "stop-20261025120000-abcd"
    before = (base / "outbox" / "stop-20261025120000-abcd.json").read_text()
    again = send(script, base, tmp_path, "stop", "--child", "other", env=env)
    assert again.returncode == 1
    assert (base / "outbox" / "stop-20261025120000-abcd.json").read_text() == before


def test_send_output_parses_with_protocol(script, base, tmp_path):
    from jailbee.orchestration.protocol import parse_manifest

    cases = [
        ("message", "--text", "hello"),
        ("spawn", "--branch", "fix-1", "--mount", "data", "--egress", "pypi.org", "--text", "go"),
        (
            "spawn",
            "--branch",
            "n",
            "--new",
            "--base",
            "main",
            "--loose",
            "--note",
            "n",
            "--text",
            "go",
        ),
        ("restart", "--child", "c", "--text", "again"),
        ("import", "--child", "c"),
        ("stop", "--child", "c"),
        ("destroy", "--child", "c"),
        ("destroy", "--child", "c", "--force"),
    ] + [
        ("message", "--kind", k, "--to", "c", "--reply-to", "h-1", "--text", "t")
        for k in ("note", "question", "done", "blocked")
    ]
    for args in cases:
        r = send(script, base, tmp_path, *args)
        assert r.returncode == 0, (args, r.stderr)
        mid = r.stdout.strip()
        files = {p.name: p.read_text() for p in (base / "outbox").glob("*.md")}
        parsed = parse_manifest(f"{mid}.json", (base / "outbox" / f"{mid}.json").read_text(), files)
        assert parsed.id == mid


def test_wait_with_many_unread_messages_does_not_die_of_sigpipe(script, base, tmp_path):
    # `unread_files | head -n 1` under pipefail killed the producer with
    # SIGPIPE (exit 141) once the listing outgrew the pipe buffer.
    inbox = base / "inbox"
    for i in range(2000):
        (inbox / f"20261025T100000Z-{i:08d}-h-0000aaaa.json").write_text("{}")
    env = env_for(base, tmp_path)
    for _ in range(3):
        r = run(script, "wait", "--timeout", "0", env=env)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert r.stdout.strip().endswith("20261025T100000Z-00000000-h-0000aaaa.json")
