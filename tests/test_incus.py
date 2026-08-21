"""Tests for the Incus CLI wrapper. Uses pytest-mock to fake subprocess calls."""

from __future__ import annotations

import json
import subprocess

import pytest

from jailbee.incus import Incus, IncusError


@pytest.fixture
def incus(mocker):
    return Incus()


def _mock_run(mocker, stdout: str = "", stderr: str = "", returncode: int = 0):
    return mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )


def test_list_returns_parsed_json(incus, mocker):
    payload = json.dumps(
        [
            {
                "name": "feat-foo",
                "status": "Running",
                "state": {
                    "network": {"eth0": {"addresses": [{"address": "10.0.0.5", "family": "inet"}]}}
                },
            },
        ]
    )
    _mock_run(mocker, stdout=payload)
    result = incus.list_containers()
    assert len(result) == 1
    assert result[0]["name"] == "feat-foo"


def test_exists_true_when_in_list(incus, mocker):
    payload = json.dumps([{"name": "feat-foo", "status": "Running"}])
    _mock_run(mocker, stdout=payload)
    assert incus.exists("feat-foo") is True


def test_exists_false_when_not_in_list(incus, mocker):
    _mock_run(mocker, stdout="[]")
    assert incus.exists("missing") is False


def test_init_calls_incus_init(incus, mocker):
    run = _mock_run(mocker)
    incus.init("gisgro-base", "feat-foo")
    args = run.call_args[0][0]
    assert args[:3] == ["incus", "init", "gisgro-base"]
    assert "feat-foo" in args


def test_copy_calls_incus_copy(incus, mocker):
    run = _mock_run(mocker)
    incus.copy("source", "dest")
    args = run.call_args[0][0]
    assert args[:3] == ["incus", "copy", "source"]
    assert "dest" in args


def test_start_calls_incus_start(incus, mocker):
    run = _mock_run(mocker)
    incus.start("feat-foo")
    args = run.call_args[0][0]
    assert args == ["incus", "start", "feat-foo"]


def test_stop_calls_incus_stop_with_force(incus, mocker):
    run = _mock_run(mocker)
    incus.stop("feat-foo", force=True)
    args = run.call_args[0][0]
    assert "incus" in args[0]
    assert "stop" in args
    assert "--force" in args


def test_stop_passes_an_explicit_clean_shutdown_timeout(incus, mocker):
    """Without `--timeout`, incusd waits 600s before failing the stop."""
    run = _mock_run(mocker)
    incus.stop("feat-foo", timeout=120)
    args = run.call_args[0][0]
    assert args == ["incus", "stop", "feat-foo", "--timeout", "120"]


def test_stop_force_never_sends_a_timeout(incus, mocker):
    """A forced stop is a zero-timeout stop server-side; both is contradictory."""
    run = _mock_run(mocker)
    incus.stop("feat-foo", force=True, timeout=120)
    args = run.call_args[0][0]
    assert "--timeout" not in args
    assert "--force" in args


def test_console_log_returns_the_console_ring_buffer(incus, mocker):
    run = _mock_run(mocker, stdout="A stop job is running for Daily apt upgrade\n")
    out = incus.console_log("feat-foo")
    assert run.call_args[0][0] == ["incus", "console", "feat-foo", "--show-log"]
    assert "stop job" in out


def test_delete_calls_incus_delete(incus, mocker):
    run = _mock_run(mocker)
    incus.delete("feat-foo", force=True)
    args = run.call_args[0][0]
    assert "delete" in args
    assert "--force" in args


def test_exec_runs_command_in_container(incus, mocker):
    _mock_run(mocker, stdout="hello\n")
    out = incus.exec("feat-foo", ["echo", "hello"])
    assert out.strip() == "hello"


def test_run_tolerates_non_utf8_output(incus):
    """`_run` must not crash when a subprocess emits non-UTF-8 bytes.

    `git diff` streams raw file bytes, so a latin-1 text file (or a
    mislabeled binary) puts stray bytes like 0xf6 (ö) into stdout. With
    strict UTF-8 decoding this raised UnicodeDecodeError and took down
    `gie diff` entirely. Uses a real subprocess — no incus, no network —
    to exercise the actual decode path the mocks skip over.
    """
    sh = Incus(binary="/bin/sh")
    # printf '\366' emits the single byte 0xf6, invalid as UTF-8.
    result = sh._run(["-c", r"printf '\366'"])
    assert result.stdout == "�"


def test_exec_passes_numeric_uid_and_gid(incus, mocker):
    run = _mock_run(mocker)
    incus.exec("feat-foo", ["whoami"], uid=53023, gid=53023)
    args = run.call_args[0][0]
    assert "--user" in args
    assert args[args.index("--user") + 1] == "53023"
    assert "--group" in args
    assert args[args.index("--group") + 1] == "53023"


def test_exec_init_groups_uses_setpriv_not_user_flags(incus, mocker):
    run = _mock_run(mocker)
    incus.exec("feat-foo", ["id"], uid=53023, gid=53023, init_groups=True)
    args = run.call_args[0][0]
    # init_groups routes through setpriv (which DOES initgroups) instead of
    # incus exec --user/--group (which does not).
    assert "--user" not in args
    assert "--group" not in args
    assert "setpriv" in args
    assert args[args.index("--reuid") + 1] == "53023"
    assert args[args.index("--regid") + 1] == "53023"
    assert "--init-groups" in args
    assert args[-1] == "id"


def test_exec_init_groups_ignored_for_root(incus, mocker):
    run = _mock_run(mocker)
    incus.exec("feat-foo", ["id"], uid=0, gid=0, init_groups=True)
    args = run.call_args[0][0]
    # root needs no group init; keep the plain --user/--group path.
    assert "setpriv" not in args
    assert "--user" in args


def test_profile_assign_uses_comma_list(incus, mocker):
    run = _mock_run(mocker)
    incus.profile_assign("feat-foo", ["default", "gisgro-base", "gisgro-binds"])
    args = run.call_args[0][0]
    # incus profile assign <container> <comma-list>
    assert "default,gisgro-base,gisgro-binds" in args


def test_profile_show_returns_stdout(incus, mocker):
    _mock_run(mocker, stdout="name: foo\nconfig: {}\n")
    assert incus.profile_show("foo") == "name: foo\nconfig: {}\n"


def test_profile_show_invokes_incus_profile_show(incus, mocker):
    run = _mock_run(mocker, stdout="name: foo\n")
    incus.profile_show("foo")
    args = run.call_args[0][0]
    assert args == ["incus", "profile", "show", "foo"]


def test_launch_without_config(incus, mocker):
    run = _mock_run(mocker)
    incus.launch("images:ubuntu/26.04", "feat-foo")
    args = run.call_args[0][0]
    assert args[:4] == ["incus", "launch", "images:ubuntu/26.04", "feat-foo"]
    assert "-c" not in args


def test_launch_with_config_emits_c_flags(incus, mocker):
    run = _mock_run(mocker)
    incus.launch(
        "images:ubuntu/26.04",
        "feat-foo",
        config={"security.nesting": "true", "limits.cpu": "4"},
    )
    args = run.call_args[0][0]
    assert "-c" in args
    assert "security.nesting=true" in args
    assert "limits.cpu=4" in args


def test_failed_command_raises_incus_error(incus, mocker):
    _mock_run(mocker, returncode=1, stderr="something broke")
    with pytest.raises(IncusError) as exc:
        incus.start("feat-foo")
    assert "something broke" in str(exc.value)


def test_timeout_is_normalized_to_incus_error(incus, mocker):
    """`subprocess.TimeoutExpired` must surface as `IncusError`.

    `incus.py` is the sole subprocess boundary, so callers only ever
    handle `IncusError` — a raw `TimeoutExpired` would crash paths like
    the git-status probe that document timeouts as a handled failure.
    """
    mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["incus", "exec"], timeout=3),
    )
    with pytest.raises(IncusError) as exc:
        incus.exec("feat-foo", ["bash", "-c", "true"], timeout=3)
    assert "timed out" in str(exc.value)
    assert "feat-foo" in str(exc.value)


def test_timeout_is_an_incus_error_subclass_callers_can_single_out(incus, mocker):
    """`IncusTimeoutError` must stay catchable as `IncusError`.

    Every other caller catches the base class and must keep catching expiries
    unchanged; only code with something specific to say about running out of
    budget — `pr_ai`, which points at the transcript Claude left behind —
    catches the subclass first.
    """
    from jailbee.incus import IncusTimeoutError

    mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["incus", "exec"], timeout=3),
    )
    with pytest.raises(IncusTimeoutError):
        incus.exec("feat-foo", ["bash", "-c", "true"], timeout=3)
    assert issubclass(IncusTimeoutError, IncusError)


def test_non_timeout_failure_is_not_an_incus_timeout(incus, mocker):
    """A non-zero exit must not be mistaken for an expiry.

    `pr_ai` decides whether a resumable transcript exists from the exception
    type alone, so a plain failure has to stay a plain `IncusError`.
    """
    from jailbee.incus import IncusTimeoutError

    mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["incus", "exec"], returncode=127, stdout="", stderr="claude: not found"
        ),
    )
    with pytest.raises(IncusError) as exc:
        incus.exec("feat-foo", ["bash", "-c", "claude"])
    assert not isinstance(exc.value, IncusTimeoutError)


def test_dry_run_does_not_call_subprocess(mocker):
    incus = Incus(dry_run=True)
    run = _mock_run(mocker)
    incus.start("feat-foo")
    run.assert_not_called()


def test_image_alias_rename(incus, mocker):
    run = _mock_run(mocker)
    incus.image_alias_rename("gisgro-base", "gisgro-base-2026-05-07")
    args = run.call_args[0][0]
    assert args == [
        "incus",
        "image",
        "alias",
        "rename",
        "gisgro-base",
        "gisgro-base-2026-05-07",
    ]


def test_network_get_returns_stripped_stdout(incus, mocker):
    _mock_run(mocker, stdout=" acl1,acl2 \n")
    result = incus.network_get("incusbr0", "security.acls")
    assert result == "acl1,acl2"


def test_network_get_returns_empty_string_when_unset(incus, mocker):
    _mock_run(mocker, stdout="\n")
    result = incus.network_get("incusbr0", "security.acls")
    assert result == ""


def test_network_set_invokes_cli(incus, mocker):
    run = _mock_run(mocker)
    incus.network_set("incusbr0", "security.acls", "myrepo-allowlist")
    args = run.call_args[0][0]
    assert args == [
        "incus",
        "network",
        "set",
        "incusbr0",
        "security.acls",
        "myrepo-allowlist",
    ]


def test_network_acl_show_returns_stdout(incus, mocker):
    yaml_out = "name: myrepo-allowlist\negress:\n- action: allow\n"
    _mock_run(mocker, stdout=yaml_out)
    result = incus.network_acl_show("myrepo-allowlist")
    assert result == yaml_out


def test_network_acl_show_invokes_cli(incus, mocker):
    run = _mock_run(mocker, stdout="name: myrepo-allowlist\n")
    incus.network_acl_show("myrepo-allowlist")
    args = run.call_args[0][0]
    assert args == ["incus", "network", "acl", "show", "myrepo-allowlist"]


def test_network_exists_returns_true_when_listed(incus, mocker):
    payload = json.dumps(
        [
            {"name": "incusbr0", "type": "bridge", "managed": True},
            {"name": "jailbee-loose", "type": "bridge", "managed": True},
        ]
    )
    _mock_run(mocker, stdout=payload)
    assert incus.network_exists("jailbee-loose") is True


def test_network_exists_returns_false_when_not_listed(incus, mocker):
    payload = json.dumps([{"name": "incusbr0", "type": "bridge", "managed": True}])
    _mock_run(mocker, stdout=payload)
    assert incus.network_exists("jailbee-loose") is False


def test_network_create_invokes_cli_with_bridge_type(incus, mocker):
    run = _mock_run(mocker)
    incus.network_create("jailbee-loose")
    args = run.call_args[0][0]
    assert args == [
        "incus",
        "network",
        "create",
        "jailbee-loose",
        "--type=bridge",
    ]


def test_network_rename_shells_out(incus, mocker):
    run = _mock_run(mocker)
    incus.network_rename("gie-loose", "jailbee-loose")
    args = run.call_args[0][0]
    assert args[:3] == ["incus", "network", "rename"]
    assert args[3:5] == ["gie-loose", "jailbee-loose"]


def test_network_delete_shells_out(incus, mocker):
    run = _mock_run(mocker)
    incus.network_delete("gie-loose")
    args = run.call_args[0][0]
    assert args == ["incus", "network", "delete", "gie-loose"]


def test_network_used_by_returns_the_referencing_objects(incus, mocker):
    payload = json.dumps(
        [
            {"name": "incusbr0", "used_by": ["/1.0/instances/other"]},
            {
                "name": "gie-loose",
                "used_by": ["/1.0/profiles/app-net-loose", "/1.0/instances/app-feat"],
            },
        ]
    )
    _mock_run(mocker, stdout=payload)
    assert incus.network_used_by("gie-loose") == [
        "/1.0/profiles/app-net-loose",
        "/1.0/instances/app-feat",
    ]


def test_network_used_by_is_empty_for_an_unused_or_missing_network(incus, mocker):
    payload = json.dumps(
        [
            {"name": "gie-loose", "used_by": []},
            {"name": "jailbee-loose"},
        ]
    )
    _mock_run(mocker, stdout=payload)
    assert incus.network_used_by("gie-loose") == []
    assert incus.network_used_by("jailbee-loose") == []
    assert incus.network_used_by("nonexistent") == []


def test_config_get_returns_value(incus, mocker):
    _mock_run(mocker, stdout="feat/foo\n")
    assert incus.config_get("feat-foo", "user.jailbee.branch") == "feat/foo"


def test_config_get_returns_none_on_empty(incus, mocker):
    _mock_run(mocker, stdout="\n")
    assert incus.config_get("feat-foo", "user.jailbee.branch") is None


def test_config_get_returns_none_on_nonzero(incus, mocker):
    _mock_run(mocker, returncode=1, stderr="not found")
    assert incus.config_get("feat-foo", "user.jailbee.branch") is None


def test_config_get_invokes_cli(incus, mocker):
    run = _mock_run(mocker, stdout="x\n")
    incus.config_get("feat-foo", "user.jailbee.branch")
    args = run.call_args[0][0]
    assert args == ["incus", "config", "get", "feat-foo", "user.jailbee.branch"]


def test_run_passes_devnull_stdin_so_terminal_input_is_not_eaten(incus, mocker):
    # Regression: `incus exec` (and other non-interactive incus commands)
    # used to inherit the parent's stdin, so `incus exec` forwarded
    # whatever the user typed at the terminal into the container. Bytes
    # the user intended for the next shell command (e.g. typing
    # `gie tmux` while `gie new` was still running) were consumed and
    # lost. Every non-interactive `_run` must pass stdin=DEVNULL.
    run = _mock_run(mocker)
    incus.start("feat-foo")
    assert run.call_args.kwargs.get("stdin") is subprocess.DEVNULL


def test_exec_passes_devnull_stdin(incus, mocker):
    # `incus exec` is the worst offender — by default it forwards stdin
    # into the container. Going through `_run` must DEVNULL stdin.
    run = _mock_run(mocker, stdout="ok\n")
    incus.exec("feat-foo", ["true"])
    assert run.call_args.kwargs.get("stdin") is subprocess.DEVNULL


def test_exec_interactive_inherits_stdin(incus, mocker):
    # The interactive path (gie shell / gie tmux / gie exec) MUST inherit
    # stdin — that's how the user's keypresses reach the shell/tmux.
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    incus.exec_interactive("feat-foo", ["bash"])
    # Either stdin is not passed, or it is None (inherit). Crucially, it
    # must NOT be DEVNULL — that would break interactive shells.
    assert run.call_args.kwargs.get("stdin") is not subprocess.DEVNULL


def test_config_unset_invokes_cli(incus, mocker):
    run = _mock_run(mocker)
    incus.config_unset("feat-foo", "user.jailbee.loose_until")
    args = run.call_args[0][0]
    assert args == ["incus", "config", "unset", "feat-foo", "user.jailbee.loose_until"]


def test_config_unset_swallows_unknown_key(incus, mocker):
    """`incus config unset` exits non-zero when the key was never set —
    treat that as success so callers can be unconditionally idempotent."""
    _mock_run(mocker, returncode=1, stderr="Error: Config option not found")
    # Should not raise.
    incus.config_unset("feat-foo", "user.jailbee.loose_until")


# ---- ETag-race retry on config read-modify-write ---------------------------
#
# `incus config device add/remove`, `config set`, and `profile assign` are
# optimistic-concurrency read-modify-writes against the instance config. A
# freshly-started container churns `volatile.*` keys asynchronously, bumping the
# config ETag mid-operation, so the PUT can fail with
# `Error: ETag doesn't match: <old> vs <new>`. The op is idempotent, so we retry.

_ETAG_ERROR = (
    "Error: ETag doesn't match: "
    "7f9e605efdd7c0dd24d3b57f5a5c3da77577b0887dce23b975924dc7e65ddb09 vs "
    "41d56c299e1e9f3312aa24812cf29f6701b6f0b225c26322593ddb16c4465047"
)


def _cp(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_config_device_add_retries_on_etag_mismatch(incus, mocker):
    """A transient ETag race must be retried, not surfaced as a failure."""
    sleep = mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr=_ETAG_ERROR), _cp(returncode=0)],
    )
    incus.config_device_add("feat-foo", "shared-x", "disk", {"source": "/a", "path": "/b"})
    assert run.call_count == 2
    sleep.assert_called_once()  # backed off between the two attempts


def test_config_device_add_raises_after_exhausting_etag_retries(incus, mocker):
    """Persistent ETag failures eventually surface as IncusError."""
    mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr=_ETAG_ERROR)] * 10,
    )
    with pytest.raises(IncusError) as exc:
        incus.config_device_add("feat-foo", "shared-x", "disk", {"source": "/a"})
    assert "ETag doesn't match" in str(exc.value)
    assert run.call_count == incus._ETAG_RETRIES


def test_config_device_add_does_not_retry_on_other_errors(incus, mocker):
    """Non-ETag failures must fail fast — retrying would mask real errors."""
    sleep = mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr="Error: device already exists")],
    )
    with pytest.raises(IncusError):
        incus.config_device_add("feat-foo", "shared-x", "disk", {"source": "/a"})
    assert run.call_count == 1
    sleep.assert_not_called()


def test_config_set_retries_on_etag_mismatch(incus, mocker):
    mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr=_ETAG_ERROR), _cp(returncode=0)],
    )
    incus.config_set("feat-foo", "user.jailbee.branch", "feat/x")
    assert run.call_count == 2


def test_config_device_remove_retries_on_etag_mismatch(incus, mocker):
    mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr=_ETAG_ERROR), _cp(returncode=0)],
    )
    incus.config_device_remove("feat-foo", "shared-x")
    assert run.call_count == 2


def test_profile_assign_retries_on_etag_mismatch(incus, mocker):
    mocker.patch("jailbee.incus.time.sleep")
    run = mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=[_cp(returncode=1, stderr=_ETAG_ERROR), _cp(returncode=0)],
    )
    incus.profile_assign("feat-foo", ["default", "gie-x"])
    assert run.call_count == 2


def test_list_images_returns_parsed_json(incus, mocker):
    payload = json.dumps(
        [
            {
                "fingerprint": "abc123",
                "size": 500,
                "aliases": [{"name": "gisgro-base", "description": ""}],
            }
        ]
    )
    _mock_run(mocker, stdout=payload)
    result = incus.list_images()
    assert len(result) == 1
    assert result[0]["aliases"][0]["name"] == "gisgro-base"


def test_list_images_empty_when_no_output(incus, mocker):
    _mock_run(mocker, stdout="")
    assert incus.list_images() == []


def test_image_delete_shells_incus_image_delete(incus, mocker):
    run = _mock_run(mocker)
    incus.image_delete("gisgro-base-2026-07-20")
    assert run.call_args.args[0] == [
        "incus",
        "image",
        "delete",
        "gisgro-base-2026-07-20",
    ]


def test_image_delete_raises_on_in_use(incus, mocker):
    _mock_run(mocker, stderr="Image is currently in use", returncode=1)
    with pytest.raises(IncusError):
        incus.image_delete("gisgro-base-2026-07-20")


def test_list_containers_fast_adds_flag_and_timeout(incus, mocker):
    """Completion queries pass --fast and a timeout; both are opt-in."""
    mock_run = _mock_run(mocker, stdout="[]")
    incus.list_containers(fast=True, timeout=2)
    assert mock_run.call_args.args[0] == [
        "incus",
        "list",
        "--format",
        "json",
        "--fast",
    ]
    assert mock_run.call_args.kwargs["timeout"] == 2


def test_list_containers_default_is_unchanged(incus, mocker):
    """Existing callers must keep the full, untimed query."""
    mock_run = _mock_run(mocker, stdout="[]")
    incus.list_containers()
    assert mock_run.call_args.args[0] == ["incus", "list", "--format", "json"]
    assert mock_run.call_args.kwargs["timeout"] is None


def test_snapshot_list_accepts_timeout(incus, mocker):
    mock_run = _mock_run(mocker, stdout="[]")
    incus.snapshot_list("myrepo-feat-foo", timeout=2)
    assert mock_run.call_args.kwargs["timeout"] == 2


# ---- error messages: readable when a long script is involved ----


def test_timeout_error_carries_the_partial_output(mocker):
    """A timed-out command got *some* way, and how far is the diagnosis.

    Discarding it left a 10-minute provisioning timeout indistinguishable
    from a DNS failure in its first second.
    """
    import subprocess

    from jailbee.incus import Incus, IncusError

    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd=["incus", "exec", "c", "--", "bash", "-c", "..."],
            timeout=600,
            output=b"Get:1 http://archive.ubuntu.com noble InRelease\n",
            stderr=b"Temporary failure resolving 'archive.ubuntu.com'\n",
        ),
    )

    with pytest.raises(IncusError) as excinfo:
        Incus().exec("c", ["bash", "-c", "apt-get update"], timeout=600)

    message = str(excinfo.value)
    assert "timed out after 600s" in message
    assert "Temporary failure resolving" in message
    assert "archive.ubuntu.com noble InRelease" in message


def test_error_messages_summarise_a_long_script_argument(mocker):
    """The mirror's provisioning script is ~60 lines passed as one argument.

    Echoing it verbatim buried the reason under a screenful of shell.
    """
    from jailbee.incus import Incus, IncusError

    script = "set -euo pipefail\n" + "\n".join(f"echo step {i}" for i in range(80))
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="podman: command not found"
        ),
    )

    with pytest.raises(IncusError) as excinfo:
        Incus().exec("jailbee-registry-mirror", ["bash", "-c", script])

    message = str(excinfo.value)
    assert "echo step 42" not in message  # the script body is gone
    assert "-byte script" in message and "81 lines" in message
    assert "jailbee-registry-mirror" in message  # ...but the container is still named
    assert "podman: command not found" in message  # ...and so is the reason


# ---- Missing `incus` binary --------------------------------------------------
#
# A host without Incus installed (or with it outside PATH) makes every
# subprocess call raise FileNotFoundError. Left unwrapped it escapes as a raw
# traceback out of whatever jailbee command the user ran — including `doctor`,
# which is the one command whose entire job is to *report* that state. Every
# caller in the codebase catches IncusError and nothing else, so the wrapper
# has to normalise it here, the same way it already normalises timeouts.


def _mock_binary_missing(mocker):
    return mocker.patch(
        "jailbee.incus.subprocess.run",
        side_effect=FileNotFoundError(2, "No such file or directory", "incus"),
    )


def test_run_raises_incus_error_when_binary_is_missing(incus, mocker):
    _mock_binary_missing(mocker)

    with pytest.raises(IncusError) as excinfo:
        incus.list_containers()

    message = str(excinfo.value)
    assert "incus" in message
    assert "not found" in message.lower()
    assert "PATH" in message


def test_exec_interactive_raises_incus_error_when_binary_is_missing(incus, mocker):
    _mock_binary_missing(mocker)

    with pytest.raises(IncusError) as excinfo:
        incus.exec_interactive("feat-foo", ["bash"])

    assert "not found" in str(excinfo.value).lower()


def test_profile_set_yaml_raises_incus_error_when_binary_is_missing(incus, mocker):
    _mock_binary_missing(mocker)

    with pytest.raises(IncusError) as excinfo:
        incus.profile_set_yaml("jailbee-base", "config: {}\n")

    assert "not found" in str(excinfo.value).lower()


def test_network_acl_set_yaml_raises_incus_error_when_binary_is_missing(incus, mocker):
    _mock_binary_missing(mocker)

    with pytest.raises(IncusError) as excinfo:
        incus.network_acl_set_yaml("jailbee-egress", "egress: []\n")

    assert "not found" in str(excinfo.value).lower()


def test_missing_binary_error_names_the_configured_binary(mocker):
    """A custom `binary=` must appear in the message, not a hardcoded `incus`."""
    _mock_binary_missing(mocker)

    with pytest.raises(IncusError) as excinfo:
        Incus(binary="/opt/incus/bin/incus").list_containers()

    assert "/opt/incus/bin/incus" in str(excinfo.value)
