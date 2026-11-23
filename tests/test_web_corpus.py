"""The recorded transcript corpus: its normalisation and the committed files.

`scripts/web_corpus.py` records genuine Claude Code sessions on a toy
repository and normalises them before they are committed. These tests pin
that nothing identifying survives: not the recording host's paths, ids or
account, and no credential.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from jailbee.transcripts.claude import ClaudeSource
from scripts.web_corpus import (
    CORPUS_DIR,
    MANIFEST,
    SHAPES,
    TOY_PROJECT,
    Identity,
    claude_project_name,
    detect_shapes,
    find_leaks,
    normalise_home,
    record,
    scan_corpus,
)

RAW_SID = "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b"


def _identity(tmp_path: Path) -> Identity:
    # Outside the host-path prefixes `find_leaks` knows, so only the identity
    # literals can catch them.
    return Identity(
        toy_dir="/srv/rec-x1y2/toy",
        home="/data/alice",
        secrets=("alice@corp.example", "build-host-17"),
    )


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _user(identity: Identity, text: str, **extra: object) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
        "parentUuid": None,
        "sessionId": RAW_SID,
        "cwd": identity.toy_dir,
        "timestamp": "2026-11-23T13:33:00.000Z",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _normalise(tmp_path: Path, records: list[dict[str, object]]) -> tuple[Identity, Path]:
    identity = _identity(tmp_path)
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", records)
    out = tmp_path / "out"
    normalise_home(raw, out, identity, [RAW_SID])
    return identity, out / "projects" / TOY_PROJECT


def test_sessions_get_stable_ids_in_recording_order(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, "first")])
    _write(raw / f"{other}.jsonl", [_user(identity, "second", sessionId=other)])
    out = tmp_path / "out"

    normalise_home(raw, out, identity, [other, RAW_SID])

    project = out / "projects" / TOY_PROJECT
    names = sorted(p.name for p in project.glob("*.jsonl"))
    assert names == [
        "00000001-0000-4000-8000-000000000001.jsonl",
        "00000002-0000-4000-8000-000000000002.jsonl",
    ]
    second = _read(project / "00000001-0000-4000-8000-000000000001.jsonl")
    assert second[0]["sessionId"] == "00000001-0000-4000-8000-000000000001"
    assert second[0]["message"] == {"role": "user", "content": "second"}


def test_paths_point_into_the_toy_repository_only(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    parent = str(Path(identity.toy_dir).parent)
    text = (
        f"edited {identity.toy_dir}/calc.py; scratch in {parent}/other;"
        f" config at {identity.home}/.claude/settings.json"
    )
    _, project = _normalise(tmp_path, [_user(identity, text)])

    [record] = _read(next(project.glob("*.jsonl")))
    assert record["cwd"] == "/work/toy"
    assert record["message"] == {
        "role": "user",
        "content": "edited /work/toy/calc.py; scratch in /work/other;"
        " config at /home/user/.claude/settings.json",
    }


def test_a_path_is_rewritten_only_where_it_ends(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    text = f"{identity.home}/x {identity.home}bob/x {identity.toy_dir}2/x {identity.toy_dir}."
    _, project = _normalise(tmp_path, [_user(identity, text)])

    [record] = _read(next(project.glob("*.jsonl")))
    assert record["message"] == {
        "role": "user",
        "content": f"/home/user/x {identity.home}bob/x /work/toy2/x /work/toy.",
    }


def test_only_transcript_records_and_fields_survive(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    assistant = {
        "type": "assistant",
        "uuid": "11111111-2222-4333-8444-555555555555",
        "sessionId": RAW_SID,
        "userType": "external",
        "entrypoint": "sdk-cli",
        "requestId": "req_011ChKwk",
        "message": {
            "id": "msg_011ChKwkYYS5",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 2, "service_tier": "standard"},
            "content": [{"type": "thinking", "thinking": "", "signature": "CAQS8AYKEAgUGAI4"}],
        },
    }
    records = [
        {"type": "session-owner", "accountUuid": "a6445300-300b-4e42-acbd-28578a5f5265"},
        {"type": "api-request-blob", "message": {"role": "system", "content": "prompt"}},
        _user(identity, "hello"),
        {
            "type": "attachment",
            "attachment": {"type": "session_context", "context": {"userEmail": "x"}},
        },
        {"type": "attachment", "attachment": {"type": "date", "date": "2026-11-23"}},
        assistant,
        {"type": "system", "subtype": "stop_hook_summary", "hookInfos": [{"command": "x"}]},
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
    ]

    _, project = _normalise(tmp_path, records)

    kept = _read(next(project.glob("*.jsonl")))
    assert [(r["type"], r.get("subtype")) for r in kept] == [
        ("user", None),
        ("attachment", None),
        ("assistant", None),
        ("system", "compact_boundary"),
    ]
    assert kept[2] == {
        "type": "assistant",
        "uuid": "00000003-0000-4000-8000-000000000003",
        "sessionId": "00000001-0000-4000-8000-000000000001",
        "message": {
            "id": "msg_000001",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "signature"}],
        },
    }


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "message",
            {"role": "user", "content": "hello", "accountName": "PRIVATE"},
            {"role": "user", "content": "hello"},
        ),
        (
            "toolUseResult",
            {"status": "completed", "account": {"name": "PRIVATE"}},
            {"status": "completed"},
        ),
        (
            "attachment",
            {"type": "date", "date": "2026-11-23", "accountName": "PRIVATE"},
            {"type": "date", "date": "2026-11-23"},
        ),
        (
            "compactMetadata",
            {
                "trigger": "manual",
                "accountName": "PRIVATE",
                "preservedSegment": {"headUuid": RAW_SID, "account": "PRIVATE"},
            },
            {
                "trigger": "manual",
                "preservedSegment": {"headUuid": "00000001-0000-4000-8000-000000000001"},
            },
        ),
        (
            "toolUseResult",
            {"file": {"content": "toy file", "accountName": "PRIVATE"}},
            {"file": {"content": "toy file"}},
        ),
        (
            "message",
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "result", "accountName": "PRIVATE"}],
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "result"}]}
                ],
            },
        ),
    ],
)
def test_nested_objects_keep_only_known_fields(
    tmp_path: Path,
    field: str,
    value: object,
    expected: object,
) -> None:
    record = _user(_identity(tmp_path), "hello", **{field: value})
    if field == "attachment":
        record["type"] = "attachment"
    _, project = _normalise(tmp_path, [record])
    [kept] = _read(next(project.glob("*.jsonl")))
    assert kept[field] == expected
    assert "PRIVATE" not in json.dumps(kept)


def test_common_tool_inputs_survive_and_are_still_scanned(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    tools = [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "TOY-MARKER"}},
        {"type": "tool_use", "name": "Write", "input": {"content": "toy text"}},
        {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.invalid/"}},
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "Read toy", "status": "pending", "activeForm": "Reading toy", "account": "PRIVATE"}
        ]}},
    ]
    _, project = _normalise(tmp_path, [_user(identity, "", message={"content": tools})])
    [kept] = _read(next(project.glob("*.jsonl")))
    expected = json.loads(json.dumps(tools))
    del expected[3]["input"]["todos"][0]["account"]
    assert kept["message"] == {"content": expected}
    assert scan_corpus(project) == {}
    for tool in tools[:3]:
        key = next(iter(tool["input"]))
        tool["input"][key] = "/srv/private"
    _, project = _normalise(tmp_path, [_user(identity, "", message={"content": tools})])
    assert scan_corpus(project)


def test_names_and_credentials_are_redacted(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    text = (
        "host build-host-17, mail alice@corp.example or bob@elsewhere.example,"
        " keys sk-ant-oat01-AbCdEf123456_789xyz ghp_1234567890abcdefghijABCDEFGHIJ123456"
        " github_pat_11ABCDEFG0123456789_abcdefghijklmnop gho_abcdefghijklmnopqrstuvwxyz0123456789"
        " header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        " opaque Bearer s3cr3tOpaqueToken0123456789"
    )
    _, project = _normalise(tmp_path, [_user(identity, text)])

    [record] = _read(next(project.glob("*.jsonl")))
    assert record["message"] == {
        "role": "user",
        "content": "host REDACTED, mail REDACTED or REDACTED,"
        " keys REDACTED REDACTED"
        " REDACTED REDACTED"
        " header Authorization: Bearer REDACTED"
        " opaque Bearer REDACTED",
    }


def test_subagents_follow_their_session_with_stable_agent_ids(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    raw = tmp_path / "raw"
    agent = "a00d6719ec73a3a88"
    call = _user(
        identity,
        "",
        toolUseResult={"status": "completed", "agentId": agent, "agentType": "general-purpose"},
    )
    _write(raw / f"{RAW_SID}.jsonl", [call])
    subagents = raw / RAW_SID / "subagents"
    _write(subagents / f"agent-{agent}.jsonl", [_user(identity, "task", agentId=agent)])
    (subagents / f"agent-{agent}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Count lines",
                "toolUseId": "toolu_011JE7JvHsPnytR9zHCwTooH",
                "requestShape": "foreground",
                "pickedEffortModel": "claude-haiku-5-5",
            }
        )
    )
    out = tmp_path / "out"

    normalise_home(raw, out, identity, [RAW_SID])

    session = out / "projects" / TOY_PROJECT / "00000001-0000-4000-8000-000000000001"
    new_agent = "a0000000000000001"
    [main] = _read(session.with_suffix(".jsonl"))
    assert main["toolUseResult"] == {
        "status": "completed",
        "agentId": new_agent,
        "agentType": "general-purpose",
    }
    [sub] = _read(session / "subagents" / f"agent-{new_agent}.jsonl")
    assert sub["agentId"] == new_agent
    meta = json.loads((session / "subagents" / f"agent-{new_agent}.meta.json").read_text())
    assert meta == {
        "agentType": "general-purpose",
        "description": "Count lines",
        "toolUseId": "toolu_000001",
    }


CLEAN = (
    "cwd /work/toy, settings /home/user/.claude, session 00000002-0000-4000-8000-000000000002,"
    " tool toolu_000003, Co-Authored-By: Claude <noreply@anthropic.com>"
)


def test_find_leaks_passes_normalised_text(tmp_path: Path) -> None:
    assert find_leaks(CLEAN, _identity(tmp_path)) == []
    assert find_leaks(CLEAN) == []


@pytest.mark.parametrize(
    "leak",
    [
        "{toy}/calc.py",
        "/srv/rec-x1y2/other",
        "-srv-rec-x1y2-toy",
        "/data/alice/notes",
        "/home/bob/notes",
        "/tmp/claude-1000",
        "/root/.claude",
        "/mnt/share/x",
        "/run/user/1000/bus",
        "build-host-17",
        "BUILD-HOST-17",
        "alice@corp.example",
        "carol@elsewhere.example",
        "sk-ant-oat01-AbCdEf123456_789xyz",
        "ghp_1234567890abcdefghijABCDEFGHIJ123456",
        "a6445300-300b-4e42-acbd-28578a5f5265",
    ],
)
def test_find_leaks_reports_each_identifying_string(tmp_path: Path, leak: str) -> None:
    identity = _identity(tmp_path)
    leak = leak.format(toy=identity.toy_dir)

    assert find_leaks(f"{CLEAN} {leak} end", identity) != []


@pytest.mark.parametrize(
    "leak",
    [
        "/srv/private/data",
        "path:/srv/private",
        "cat </srv/private/customer-data",
        "file:///srv/private/customer-data",
        "cat </secret",
        "/opt/company/config",
        "/workspace/private/repo",
        "/var/lib/private/state",
        "/data/private",
        "/home/user2/file",
        "/work/toy-other/file",
        "/work/toy/../../private",
        "/work/toy/../private",
        "Author: Fictional Maintainer <toy@example.invalid>",
        "Commit: Another Person <noreply@anthropic.com>",
        '"authorName": "Fictional Maintainer"',
        "GIT_AUTHOR_IDENT=Fictional Maintainer <toy@example.invalid> 123 +0000",
        "user.name Fictional Maintainer",
        "git author is Fictional Maintainer",
        '"cwd": "/work/claude-tmp"',
    ],
)
def test_scan_corpus_rejects_non_placeholder_identity(tmp_path: Path, leak: str) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"message": {"content": f"output: {leak}"}}) + "\n")

    assert list(scan_corpus(tmp_path)) == [path.name]


@pytest.mark.parametrize(
    "text",
    [
        "/dev/null /usr/bin/env /usr/bin/python3",
        "https://example.invalid/docs/page https://example.invalid/",
        '"cwd": "/work/toy/nested"',
        "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
    ],
)
def test_find_leaks_allows_system_paths_urls_and_toy_subdirectories(text: str) -> None:
    assert find_leaks(text) == []


@pytest.mark.parametrize("path", [r"C:\Users\x\secret", r"\\server\share\x", "~alice/private"])
def test_scan_corpus_rejects_windows_and_named_home_paths(tmp_path: Path, path: str) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"message": {"content": path}}) + "\n")
    assert list(scan_corpus(tmp_path)) == [transcript.name]


@pytest.mark.parametrize(
    "credential",
    [
        "sk-0123456789abcdefghijklmnopqrstuvwxyz",
        "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef",
        "AIzaSy0123456789abcdefghijklmnopqrstuvwx",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "dGhpcyBpcyBhIHN5bnRoZXRpYyBzZWNyZXQgYmxvYiBmb3IgdGhlIHRlc3Q=",
    ],
)
def test_credentials_are_redacted_and_detected(tmp_path: Path, credential: str) -> None:
    assert find_leaks(f"token: {credential}")
    _, project = _normalise(tmp_path, [_user(_identity(tmp_path), f"token: {credential}")])
    [record] = _read(next(project.glob("*.jsonl")))
    assert record["message"] == {"role": "user", "content": "token: REDACTED"}


def test_identity_redaction_preserves_commands_and_tool_names(tmp_path: Path) -> None:
    identity = Identity(
        toy_dir="/recording/toy", home="/home/dev", secrets=("dev", "Will Read")
    )
    raw = tmp_path / "raw"
    text = "npm run dev; /dev/null; dev-ork; Read Edit Bash; Will Read; user dev; /home/dev/x"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, text)])
    out = tmp_path / "out"
    normalise_home(raw, out, identity, [RAW_SID])
    [kept] = _read(next((out / "projects" / TOY_PROJECT).glob("*.jsonl")))
    assert kept["message"] == {
        "role": "user",
        "content": "npm run dev; /dev/null; dev-ork; Read Edit Bash; REDACTED; user REDACTED; /home/user/x",
    }
    assert find_leaks("npm run dev; dev-ork; Read Edit Bash", identity) == []
    assert find_leaks("user dev", identity)
    assert find_leaks("Will Read", identity)


def test_short_secrets_match_whole_words_only(tmp_path: Path) -> None:
    identity = Identity(toy_dir="/recording/toy", home="/recording/home", secrets=("ann",))
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, "user ann user ANN annual banner")])
    out = tmp_path / "out"
    normalise_home(raw, out, identity, [RAW_SID])
    [kept] = _read(next((out / "projects" / TOY_PROJECT).glob("*.jsonl")))
    assert kept["message"] == {"role": "user", "content": "user REDACTED user REDACTED annual banner"}
    assert find_leaks("annual banner", identity) == []
    assert find_leaks("user ann", identity)


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("custom_config", [False, True])
@pytest.mark.parametrize("broken_config", [False, True])
def test_record_redacts_effective_git_identity_and_cleans_its_state(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
    custom_config: bool,
    broken_config: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "claude-config" if custom_config else home / ".claude"
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if custom_config:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    snapshots = config / "shell-snapshots"
    snapshots.mkdir()
    (snapshots / "old.sh").write_text("keep")
    config_file = (config if custom_config else home) / ".claude.json"
    config_file.write_text(json.dumps({"projects": {"/existing": {"keep": True}}, "other": 1}))
    mocker.patch("scripts.web_corpus.shutil.which", return_value="/bin/claude")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, "2.1.0 Claude Code", "")
        identity = {
            "GIT_AUTHOR_IDENT": "Will Read <author@example.invalid> 123 +0000",
            "GIT_COMMITTER_IDENT": "Zed Committer <committer@example.invalid> 123 +0000",
        }
        output = identity.get(command[-1], "") if command[:2] == ["git", "var"] else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    mocker.patch("scripts.web_corpus.subprocess.run", side_effect=run)

    def start(command: list[str], **kwargs: Any) -> Any:
        toy = kwargs["cwd"]
        sid = (
            command[command.index("--session-id") + 1]
            if "--session-id" in command
            else command[command.index("--resume") + 1]
        )
        project = config / "projects" / claude_project_name(str(toy))
        _write(
            project / f"{sid}.jsonl",
            [
                {
                    "type": "user",
                    "cwd": str(toy),
                    "sessionId": sid,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Will Read Zed Committer Read Edit Bash"
                                " author@example.invalid committer@example.invalid",
                            },
                            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}},
                        ],
                    },
                }
            ],
        )
        if broken_config:
            config_file.write_text("{invalid")
        else:
            data = json.loads(config_file.read_text())
            data["projects"][str(toy)] = {"private": "recording"}
            config_file.write_text(json.dumps(data))
        (snapshots / f"{sid}.sh").write_text("private shell")
        (snapshots / "snapshot-own.sh").write_text(f"export HOME={kwargs['env']['HOME']}\n")
        (snapshots / "snapshot-foreign.sh").write_text("export HOME=/home/someone-else\n")
        process = mocker.Mock()
        process.communicate.return_value = (None, "failed" if fail else "")
        process.returncode = 1 if fail else 0
        process.poll.return_value = None
        return process

    mocker.patch("scripts.web_corpus.subprocess.Popen", side_effect=start)
    original_temp = tempfile.NamedTemporaryFile
    replace = mocker.spy(os, "replace")

    def create_temp(*args: Any, **kwargs: Any) -> Any:
        # A different session updates unrelated config after cleanup's first read.
        data = json.loads(config_file.read_text())
        data["concurrent"] = 2
        config_file.write_text(json.dumps(data))
        return original_temp(*args, **kwargs)

    mocker.patch("scripts.web_corpus.tempfile.NamedTemporaryFile", side_effect=create_temp)
    out = tmp_path / "corpus"
    if broken_config:
        with pytest.raises(SystemExit, match="claude exited" if fail else "cleanup"):
            record(out)
        if fail:
            assert "cleanup failed" in capsys.readouterr().err
        assert config_file.read_text() == "{invalid"
    elif fail:
        with pytest.raises(SystemExit, match="claude exited"):
            record(out)
    else:
        record(out)
        text = "".join(p.read_text() for p in out.rglob("*.jsonl"))
        assert all(secret not in text for secret in ("Will Read", "Zed Committer"))
        assert "Read Edit Bash" in text
    if not broken_config:
        assert json.loads(config_file.read_text()) == {
            "projects": {"/existing": {"keep": True}},
            "other": 1,
            "concurrent": 2,
        }
        assert replace.call_count == 1
        assert replace.call_args.args[1] == config_file
    assert sorted(p.name for p in snapshots.iterdir()) == ["old.sh", "snapshot-foreign.sh"]
    assert not list((config / "projects").iterdir())


# --- The committed corpus ----------------------------------------------------


def _manifest() -> dict[str, Any]:
    data = json.loads((CORPUS_DIR / MANIFEST).read_text())
    assert isinstance(data, dict)
    return data


def test_committed_corpus_identifies_no_one() -> None:
    assert scan_corpus(CORPUS_DIR) == {}
    host = socket.gethostname()
    if len(host) >= 5:
        assert not any(host in path.read_text() for path in CORPUS_DIR.rglob("*") if path.is_file())


def test_a_leak_injected_into_the_corpus_is_caught(tmp_path: Path) -> None:
    copy = tmp_path / "corpus"
    shutil.copytree(CORPUS_DIR, copy)
    session = sorted((copy / "projects" / TOY_PROJECT).glob("*.jsonl"))[0]
    lines = session.read_text().splitlines()
    record = json.loads(lines[-1])
    record["cwd"] = "/home/maintainer/private-repo"
    session.write_text("\n".join([*lines[:-1], json.dumps(record)]) + "\n")

    assert list(scan_corpus(copy)) == [str(session.relative_to(copy))]


def test_manifest_matches_the_recorded_sessions() -> None:
    manifest = _manifest()
    project = CORPUS_DIR / "projects" / TOY_PROJECT
    sessions = manifest["sessions"]
    assert sorted(p.stem for p in project.glob("*.jsonl")) == sorted(
        s["session_id"] for s in sessions
    )
    covered: set[str] = set()
    for session in sessions:
        transcript = project / f"{session['session_id']}.jsonl"
        shapes = detect_shapes(transcript)
        assert session["shapes"] == shapes, session["name"]
        covered.update(shapes)
        assert session["markers"] and all(m in transcript.read_text() for m in session["markers"])
        for subagent in session["subagents"]:
            agent = transcript.with_suffix("") / "subagents" / f"agent-{subagent['agent_id']}"
            assert agent.with_suffix(".jsonl").is_file()
            assert subagent["tool_use_id"] in transcript.read_text()
    assert sorted(covered) == sorted(set(SHAPES) - set(manifest["missing_shapes"]))


def test_the_reader_lists_and_reads_every_recorded_session() -> None:
    source = ClaudeSource()
    listed = {info.session_id: info for info in source.list_sessions(CORPUS_DIR)}
    for session in _manifest()["sessions"]:
        info = listed[session["session_id"]]
        assert info.title is not None and info.title.startswith("CORPUS-")
        files = source.locate(CORPUS_DIR, session["session_id"])
        assert files is not None
        for path in [files.transcript, *files.subagents.values()]:
            assert source.read(path).entries, path


def test_claude_scratch_paths_name_the_toy_project_only(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity = Identity(
        toy_dir=identity.toy_dir,
        home=identity.home,
        renames=(("/var/cache/claude-53023", "/work/claude-tmp"),),
    )
    encoded = "-" + identity.toy_dir.strip("/").replace("/", "-").replace("_", "-")
    output = f"/var/cache/claude-53023/{encoded}/{RAW_SID}/tasks/b1.output"
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, f"written to {output}")])
    normalise_home(raw, tmp_path / "out", identity, [RAW_SID])

    [record] = _read(next((tmp_path / "out" / "projects" / TOY_PROJECT).glob("*.jsonl")))
    assert record["message"] == {
        "role": "user",
        "content": "written to /work/claude-tmp/-work-toy/"
        "00000001-0000-4000-8000-000000000001/tasks/b1.output",
    }
    assert find_leaks("/var/cache/claude-53023", identity) != []
