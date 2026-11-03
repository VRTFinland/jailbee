"""Compare the Python CLI directly with the unchanged shell implementation."""

import importlib.resources
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

CASES = [
    ["send", "message", "--text", "hello", "--to", "kid", "--kind", "question"],
    ["send", "spawn", "--branch", "b", "--text", "go"],
    ["send", "spawn", "--pr", "42", "--text", "go"],
    ["send", "spawn", "--branch", "b", "--new", "--base", "main", "--text", "go"],
    ["send", "import", "--child", "c"],
    ["send", "update", "--child", "c", "--merge"],
    ["send", "restart", "--child", "c", "--text", "go", "--resume", "last"],
    ["send", "refresh", "--child", "c", "--services", "a,b"],
    ["send", "stop", "--child", "c"],
    ["send", "destroy", "--child", "c", "--force"],
    ["send", "rights", "--mount", "x", "--for", "1h"],
    ["send", "rights", "--revoke-loose"],
    ["send", "message", "--text", "go", "--wait", "--timeout", "0"],
    ["ls"],
    ["read", "m1", "--keep"],
    ["read", "m2"],
    ["read", "unknown"],
    ["children"],
    ["children", "--json"],
    ["runners"],
    ["runners", "--json"],
    ["wait", "--timeout", "0"],
    ["send"],
    ["send", "bad"],
    ["send", "message"],
    ["send", "message", "--text", ""],
    ["send", "message", "--text"],
    ["send", "message", "--bogus"],
    ["send", "message", "--text", "x", "--kind", "bad"],
    ["send", "message", "--text", "x", "--merge"],
    ["send", "message", "--text", "x", "--timeout", "1"],
    ["send", "message", "--text", "x", "--resume", "last"],
    ["send", "message", "--text", "x", "--agent-only"],
    ["send", "message", "--text", "x", "--services", "a"],
    ["send", "message", "--text", "x", "--on-done", "keep"],
    ["send", "spawn", "--text", "x"],
    ["send", "spawn", "--pr", "0", "--text", "x"],
    ["send", "spawn", "--branch", "b", "--base", "x", "--text", "x"],
    ["send", "spawn", "--branch", "b", "--agent", "bad", "--text", "x"],
    ["send", "spawn", "--branch", "b", "--profile", "p", "--text", "x"],
    ["send", "spawn", "--branch", "b", "--agent", "claude", "--model", "bad", "--text", "x"],
    ["send", "update"],
    ["send", "update", "--child", "c", "--ref", "a", "--base", "b"],
    ["send", "refresh", "--child", "c"],
    ["send", "rights"],
    ["send", "rights", "--loose"],
    ["send", "rights", "--for", "1h"],
    ["send", "rights", "--loose", "--revoke-loose", "--for", "1h"],
    ["send", "restart", "--child", "c", "--text", "x", "--resume", "INVALID"],
    ["send", "stop", "--child", "c", "--text", "x"],
    ["send", "stop", "--child", "c", "--force"],
    ["send", "spawn", "--pr", "9" * 4301, "--text", "x"],
    ["send", "restart", "--child", "c", "--text", "x", "--agent-only", "--autostart-profile", "p"],
    ["send", "restart", "--child", "c", "--text", "x", "--agent-only", "--services", "a"],
    ["send", "spawn", "--branch", "b", "--text", "x", "--on-done", "bad"],
    ["send", "spawn", "--branch", "b", "--text", "x", "--to", "x"],
    ["send", "message", "--text", "x", "--branch", "b"],
    ["send", "message", "--text", "x", "--ref", "b"],
    ["send", "message", "--text", "x", "--agent", "claude"],
    ["send", "spawn", "--branch", "b", "--text", "x", "--default-runner"],
    ["send", "restart", "--child", "c", "--text", "x", "--default-runner", "--agent", "claude"],
    ["send", "spawn", "--branch", "b", "--text", "x", "--agent", "claude", "--profile", "p"],
    ["send", "message", "--text", "x", "--autostart-profile", "p"],
    ["send", "message", "--text", "x", "--child", "c"],
    ["send", "rights", "--mount", "a", "--revoke-mount", "a", "--for", "1h"],
    ["send", "rights", "--egress", "a", "--revoke-egress", "a", "--for", "1h"],
    ["send", "message", "--text", "x", "--for", "1h"],
    ["send", "spawn", "--branch", "b", "--text", "x", "--ref", "a", "--new"],
    ["send", "spawn", "--text", "x", "--pr", "1", "--new"],
    ["send", "message", "--text", "x", "--to", ""],
    ["send", "message", "--text", "x", "--text", "x", "-"],
    ["send", "message", "--text", "x", "--wait", "--timeout", "invalid"],
    ["send", "message", "--file", "{missing}"],
    ["send", "message", "--file", "{empty}"],
    ["send", "message", "--file", "{large}"],
    ["send", "message", "--file", "{body}"],
    ["send", "message", "-"],
]


def normalize(text):
    text = text.replace("jailbee-inbox", "jailbee inbox")
    text = re.sub(r"(\w+)-\d{14}-[0-9a-f]{4}", r"\1-ID", text)
    # Python help alone documents its intentional [1m] suffix extension.
    text = text.replace("haiku (optional [1m])]; see `runners`.", "haiku]; see `runners`.")
    text = text.replace("         Listed model tiers also accept the [1m] suffix.\n", "")
    return text.replace("JAILBEE_INBOX_INOTIFY=0 forces polling.\n", "")


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("args", CASES)
def test_impls_agree(tmp_path, args):
    base = tmp_path / "orch"
    script = tmp_path / "helper"
    script.write_text(
        importlib.resources.files("jailbee.provision").joinpath("jailbee-inbox").read_text()
    )
    for name, content in (("empty", ""), ("large", "x" * 65537), ("body", "hello")):
        (tmp_path / name).write_text(content)
    args = [
        arg.format(**{name: str(tmp_path / name) for name in ("missing", "empty", "large", "body")})
        for arg in args
    ]
    config = tmp_path / "litellm.json"
    config.write_text(
        json.dumps(
            {"default_profile": "p", "profiles": {"p": {"tiers": {"opus": "alias\x7f\x9b"}}}}
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "claude").chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "JAILBEE_ORCHESTRATION_DIR": str(base),
        "JAILBEE_LITELLM_CONFIG": str(config),
        "JAILBEE_INBOX_SETTLE": "0",
    }
    results = []
    for argv in (["bash", str(script)], [sys.executable, "-m", "jailbee.inbox"]):
        if base.exists():
            shutil.rmtree(base)
        (base / "inbox").mkdir(parents=True)
        for name, doc in [
            ("a", {"id": "m1", "type": "message", "kind": False, "body": "ä" * 70 + "\x7f\x9b"}),
            ("b", {"id": "m2", "type": "message", "body": {"x": "\x7f\x9b"}}),
        ]:
            (base / "inbox" / (name + ".json")).write_text(json.dumps(doc))
        (base / "children.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": 1,
                    "children": [{"name": "x\x7f\x9b", "running": True, "session": "s"}],
                }
            )
        )
        result = subprocess.run(
            [*argv, *args], env=env, input="stdin body", capture_output=True, text=True, timeout=30
        )
        if args == ["runners"]:
            assert "alias??" in result.stdout
        if args == ["runners", "--json"]:
            assert "alias\\u007f?" in result.stdout
        files = sorted(
            (normalize(p.name), normalize(p.read_text())) for p in (base / "outbox").glob("*")
        )
        results.append(
            (result.returncode, normalize(result.stdout), normalize(result.stderr), files)
        )
    assert results[0] == results[1]
