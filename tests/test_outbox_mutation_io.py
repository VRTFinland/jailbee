"""Run the exact shell helper via a local, mocked Incus transport."""

import os
import subprocess

import pytest

from jailbee.incus import IncusError, IncusTimeoutError
from jailbee.outbox.models import OutboxExecutionError


def local_mutator(mocker, tmp_path):
    from jailbee.outbox import io

    directory = tmp_path / "home" / "dev" / ".jailbee" / "pr-outbox"
    directory.mkdir(parents=True)
    incus = mocker.Mock()

    def execute(container, command, input_text, **kwargs):
        # Substitute only the fixed directory argv; never rewrite script source.
        command = list(command)
        command[4] = str(directory)
        result = subprocess.run(
            command, input=input_text, text=True, capture_output=True, timeout=10
        )
        if result.returncode:
            raise IncusError(result.stderr)
        return result.stdout

    incus.exec_with_input.side_effect = execute
    return io, incus, directory


def mutate(
    env,
    expected,
    replacement=("001.json", "replacement"),
    delete=(),
    progress="001.json.progress.json",
):
    io, incus, _ = env
    return io.mutate_store(
        incus,
        "c",
        "pr",
        uid=1000,
        expected=expected,
        new_manifest=replacement,
        delete_names=delete,
        forbidden_progress=progress,
    )


def test_atomic_create_preserves_inventory_and_never_follows_late_symlink(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("proposal")
    mutate(env, {"001.json": "proposal"}, ("applied.log", "receipt\n"), progress=None)
    assert (env[2] / "applied.log").read_text() == "receipt\n"
    (env[2] / "applied.log").unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (env[2] / "applied.log").symlink_to(outside)
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "proposal"}, ("applied.log", "receipt\n"), progress=None)
    assert outside.read_text() == "untouched"


def test_large_stdin_is_not_silently_truncated(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("old")
    replacement = "x" * 200000
    mutate(env, {"001.json": "old"}, ("001.json", replacement))
    assert (env[2] / "001.json").read_text() == replacement


def test_utf8_stdin_atomic_replace_and_sibling_preservation(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    target = env[2] / "001.json"
    target.write_text("original\n")
    (env[2] / "neighbor.json").write_text("neighbor")
    expected = {"001.json": "original\n", "neighbor.json": "neighbor"}
    assert mutate(env, expected, ("001.json", "Unicode: ä\n$(touch hacked)")) == ()
    assert target.read_text() == "Unicode: ä\n$(touch hacked)"
    assert (env[2] / "neighbor.json").read_text() == "neighbor"
    assert not (env[2] / "hacked").exists()
    assert not list(env[2].glob(".outbox-*.tmp"))
    assert env[1].exec_with_input.call_args.kwargs == {
        "uid": 1000,
        "timeout": env[0].MUTATION_TIMEOUT,
    }


@pytest.mark.parametrize(
    "unsafe", ["hash", "symlink", "fifo", "hardlink", "missing", "progress", "sibling"]
)
def test_unsafe_final_evidence_leaves_target_unchanged(mocker, tmp_path, unsafe):
    env = local_mutator(mocker, tmp_path)
    target = env[2] / "001.json"
    target.write_text("original")
    expected = {"001.json": "wrong" if unsafe == "hash" else "original"}
    outside = tmp_path / "outside"
    outside.write_text("original")
    if unsafe in ("symlink", "fifo", "hardlink", "missing"):
        target.unlink()
        if unsafe == "symlink":
            target.symlink_to(outside)
        elif unsafe == "fifo":
            os.mkfifo(target)
        elif unsafe == "hardlink":
            os.link(outside, target)
    elif unsafe == "progress":
        (env[2] / "001.json.progress.json").write_text('{"applied":[],"urls":{}}')
    elif unsafe == "sibling":
        (env[2] / "002.json").write_text('{"body_file":"body.md"}')
    with pytest.raises(OutboxExecutionError):
        mutate(env, expected)
    if unsafe not in ("missing", "fifo"):
        assert target.read_text() == "original"
    assert outside.read_text() == "original"
    assert not list(env[2].glob(".outbox-*.tmp"))


def test_new_sibling_in_final_window_keeps_exclusive_body(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    for name, text in {"001.json": "manifest", "body.md": "body"}.items():
        (env[2] / name).write_text(text)
    original_execute = env[1].exec_with_input.side_effect

    def late_sibling(*args, **kwargs):
        (env[2] / "002.json").write_text('{"actions":[{"body_file":"body.md"}]}')
        return original_execute(*args, **kwargs)

    env[1].exec_with_input.side_effect = late_sibling
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "manifest", "body.md": "body"}, None, ("001.json", "body.md"))
    assert (env[2] / "body.md").read_text() == "body"
    assert (env[2] / "001.json").exists()


@pytest.mark.parametrize("ancestor", ["home", "dev", ".jailbee", "pr-outbox"])
def test_symlink_ancestor_refuses(mocker, tmp_path, ancestor):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    target = next(p for p in (env[2], *env[2].parents) if p.name == ancestor)
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved)
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "original"


def fake_binary(tmp_path, monkeypatch, name, script):
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    binary = folder / name
    binary.write_text("#!/bin/bash\n" + script)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])


@pytest.mark.parametrize("command", ["mktemp", "stat"])
def test_initialization_warning_refuses_before_mutation(mocker, tmp_path, monkeypatch, command):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    import shlex

    marker = shlex.quote(str(tmp_path / "first-call"))
    fake_binary(
        tmp_path,
        monkeypatch,
        command,
        f"if [[ ! -e {marker} ]]; then touch {marker}; "
        'printf "initialization warning" >&2; fi\n'
        f'exec /usr/bin/{command} "$@"\n',
    )
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "original"
    assert not list(env[2].glob(".outbox-*.tmp"))


def test_cleanup_warning_is_not_success(mocker, tmp_path, monkeypatch):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    # Inject stderr at the real cleanup command, without rewriting the helper.
    hook = tmp_path / "hook"
    hook.write_text(
        "set -T\n"
        'trap \'if [[ $BASH_COMMAND == *"/bin/rm -f"* ]]; then '
        'printf "cleanup warning" >&2; fi\' DEBUG\n'
    )
    monkeypatch.setenv("BASH_ENV", str(hook))
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "replacement"
    assert not list(env[2].glob(".outbox-*.tmp"))


def test_temp_write_failure_retains_target(mocker, tmp_path, monkeypatch):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    fake_binary(
        tmp_path,
        monkeypatch,
        "dd",
        'if [[ "$*" == *of=* ]]; then printf "write failed" >&2; exit 1; fi\n'
        'exec /usr/bin/dd "$@"\n',
    )
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "original"
    assert not list(env[2].glob(".outbox-*.tmp"))


def test_unexpected_success_stderr_fails_without_removing(mocker, tmp_path, monkeypatch):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    fake_binary(
        tmp_path,
        monkeypatch,
        "sha256sum",
        "printf 'unexpected internal warning' >&2\nexec /usr/bin/sha256sum \"$@\"\n",
    )
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "original"


def test_partial_remove_reports_actual_names(mocker, tmp_path, monkeypatch):
    env = local_mutator(mocker, tmp_path)
    expected = {"001.json": "manifest", "body.md": "body"}
    for name, text in expected.items():
        (env[2] / name).write_text(text)
    fake_binary(
        tmp_path,
        monkeypatch,
        "rm",
        'if [[ "$*" == *body.md* ]]; then printf "denied" >&2; exit 1; fi\nexec /usr/bin/rm "$@"\n',
    )
    with pytest.raises(OutboxExecutionError) as failure:
        mutate(env, expected, None, ("001.json", "body.md"))
    assert failure.value.removed_names == ("001.json",)
    assert not (env[2] / "001.json").exists()
    assert (env[2] / "body.md").exists()


def test_whole_removal_and_full_progress_log_hash(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    expected = {"001.json": "manifest", "body.md": "body", "applied.log": "unrelated receipt\n"}
    for name, text in expected.items():
        (env[2] / name).write_text(text)
    (env[2] / "applied.log").write_text("changed receipt\n")
    with pytest.raises(OutboxExecutionError):
        mutate(env, expected, None, ("001.json", "body.md"))
    (env[2] / "applied.log").write_text(expected["applied.log"])
    assert mutate(env, expected, None, ("001.json", "body.md")) == ("001.json", "body.md")
    assert (env[2] / "applied.log").read_text() == expected["applied.log"]


@pytest.mark.parametrize("raw", ["", "v1\0ok\0", "garbage", "x" * (1024 * 1024)])
def test_malformed_bounded_result_fails(mocker, raw):
    from jailbee.outbox.io import mutate_store

    incus = mocker.Mock()
    incus.exec_with_input.return_value = raw
    with pytest.raises(OutboxExecutionError):
        mutate_store(
            incus,
            "c",
            "issue",
            uid=None,
            expected={"001.json": "old"},
            new_manifest=("001.json", "new"),
            delete_names=(),
            forbidden_progress=None,
        )


def test_sibling_created_during_final_hash_refuses_cleanup(mocker, tmp_path, monkeypatch):
    import shlex

    env = local_mutator(mocker, tmp_path)
    expected = {"001.json": "manifest", "body.md": "body"}
    for name, text in expected.items():
        (env[2] / name).write_text(text)
    # Count hash calls: first full validation sees two files; final validation
    # gains a sibling after inventory enumeration, while hashing its last file.
    counter = tmp_path / "count"
    sibling = env[2] / "002.json"
    fake_binary(
        tmp_path,
        monkeypatch,
        "sha256sum",
        f"n=0; [[ ! -f {shlex.quote(str(counter))} ]] || "
        f"read -r n < {shlex.quote(str(counter))}\n"
        f'n=$((n+1)); printf "%s\\n" "$n" > {shlex.quote(str(counter))}\n'
        f'if (( n == 4 )); then printf "sibling" > {shlex.quote(str(sibling))}; fi\n'
        'exec /usr/bin/sha256sum "$@"\n',
    )
    with pytest.raises(OutboxExecutionError):
        mutate(env, expected, None, ("001.json", "body.md"))
    assert (env[2] / "body.md").read_text() == "body"
    assert (env[2] / "001.json").read_text() == "manifest"


def test_sibling_created_between_unlinks_retains_body(mocker, tmp_path, monkeypatch):
    import shlex

    env = local_mutator(mocker, tmp_path)
    expected = {"001.json": "manifest", "body.md": "body"}
    for name, text in expected.items():
        (env[2] / name).write_text(text)
    sibling = env[2] / "002.json"
    fake_binary(
        tmp_path,
        monkeypatch,
        "rm",
        f'/usr/bin/rm "$@" || exit $?\nprintf "sibling" > {shlex.quote(str(sibling))}\n',
    )
    with pytest.raises(OutboxExecutionError) as failure:
        mutate(env, expected, None, ("001.json", "body.md"))
    assert failure.value.removed_names == ("001.json",)
    assert (env[2] / "body.md").read_text() == "body"


@pytest.mark.parametrize("special", ["symlink", "fifo", "hardlink", "directory"])
@pytest.mark.parametrize("name", ["001.json.progress.json", "applied.log"])
def test_unsafe_progress_blocks_without_opening(mocker, tmp_path, special, name):
    env = local_mutator(mocker, tmp_path)
    (env[2] / "001.json").write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("evidence")
    evidence = env[2] / name
    if special == "symlink":
        evidence.symlink_to(outside)
    elif special == "fifo":
        os.mkfifo(evidence)
    elif special == "hardlink":
        os.link(outside, evidence)
    else:
        evidence.mkdir()
    with pytest.raises(OutboxExecutionError):
        mutate(env, {"001.json": "original"})
    assert (env[2] / "001.json").read_text() == "original"


def test_changed_neighbor_text_preserves_body(mocker, tmp_path):
    env = local_mutator(mocker, tmp_path)
    expected = {"001.json": "manifest", "body.md": "body", "002.json": "old neighbor"}
    for name, text in expected.items():
        (env[2] / name).write_text(text)
    (env[2] / "002.json").write_text('{"body_file":"body.md"}')
    with pytest.raises(OutboxExecutionError):
        mutate(env, expected, None, ("001.json", "body.md"))
    assert (env[2] / "body.md").read_text() == "body"


def test_timeout_is_not_success(mocker):
    from jailbee.outbox.io import mutate_store

    incus = mocker.Mock()
    incus.exec_with_input.side_effect = IncusTimeoutError("timeout")
    with pytest.raises(OutboxExecutionError):
        mutate_store(
            incus,
            "c",
            "issue",
            uid=None,
            expected={"001.json": "old"},
            new_manifest=None,
            delete_names=("001.json",),
            forbidden_progress=None,
        )
