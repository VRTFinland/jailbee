"""Runs the `jailbee-inbox` script install.sh writes against a temporary
orchestration directory. Needs bash and jq (both in the golden image)."""

import importlib.resources
import json
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
    for tool in ("jq", "sleep", "ls", "mv", "mkdir", "date", "cat", "head", "sort", "env", "bash"):
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
