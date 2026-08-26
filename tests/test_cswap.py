"""Tests for the `cswap` (claude-swap) subprocess wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jailbee.cswap import CSWAP_BINARY, Cswap, CswapError, CswapMissingError, CswapTimeoutError

_LIST_PAYLOAD = {
    "schemaVersion": 1,
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1,
            "email": "work@gisgro.com",
            "organizationName": "Gisgro",
            "organizationUuid": "org-abc",
            "isOrganization": True,
            "active": True,
            "alias": "work",
            "usageStatus": "ok",
            "usage": {
                "fiveHour": {"pct": 34.0, "resetsAt": "2026-08-26T23:29:59Z"},
                "sevenDay": {"pct": 61.5, "resetsAt": "2026-08-30T17:59:59Z"},
            },
        },
        {
            "number": 2,
            "email": "me@example.com",
            "organizationName": "",
            "organizationUuid": "",
            "isOrganization": False,
            "active": False,
            "usageStatus": "relogin_required",
            "usage": None,
            "disabled": True,
        },
    ],
}


def _completed(stdout: str = "", stderr: str = "", code: int = 0):
    return subprocess.CompletedProcess(["cswap"], code, stdout=stdout, stderr=stderr)


def _cswap(tmp_path: Path) -> Cswap:
    return Cswap(config_home=tmp_path / "shared" / "claude")


# --- environment -------------------------------------------------------


def test_claude_config_dir_is_the_shared_claude_dir(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(_LIST_PAYLOAD))
    )
    home = tmp_path / "shared" / "claude"

    Cswap(config_home=home).list_accounts()

    env = run.call_args.kwargs["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(home)


def test_home_is_not_rewritten(tmp_path, mocker, monkeypatch):
    """cswap's own store must resolve normally under the user's HOME/XDG."""
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/tester/.local/share")
    run = mocker.patch(
        "jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(_LIST_PAYLOAD))
    )

    _cswap(tmp_path).list_accounts()

    env = run.call_args.kwargs["env"]
    assert env["HOME"] == "/home/tester"
    assert env["XDG_DATA_HOME"] == "/home/tester/.local/share"


def test_securestorage_override_is_removed_from_the_child_env(tmp_path, mocker, monkeypatch):
    """A user-exported CLAUDE_SECURESTORAGE_CONFIG_DIR would make cswap read a
    different profile's credential than the one CLAUDE_CONFIG_DIR names."""
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/somewhere/else")
    run = mocker.patch(
        "jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(_LIST_PAYLOAD))
    )

    _cswap(tmp_path).list_accounts()

    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in run.call_args.kwargs["env"]


# --- list --------------------------------------------------------------


def test_list_accounts_parses_identity_alias_and_quota(tmp_path, mocker):
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(_LIST_PAYLOAD)))

    accounts = _cswap(tmp_path).list_accounts()

    assert [a.number for a in accounts] == [1, 2]
    first, second = accounts
    assert first.identity == ("work@gisgro.com", "org-abc")
    assert first.alias == "work"
    assert first.label == "work"
    assert first.active is True
    assert first.five_hour_pct == 34.0
    assert first.seven_day_pct == 61.5
    assert second.identity == ("me@example.com", "")
    assert second.alias == ""
    assert second.label == "me@example.com"
    assert second.disabled is True
    assert second.usage_status == "relogin_required"
    assert second.five_hour_pct is None and second.seven_day_pct is None


def test_list_accounts_is_empty_when_nothing_is_pooled(tmp_path, mocker):
    payload = {"schemaVersion": 1, "activeAccountNumber": None, "accounts": []}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    assert _cswap(tmp_path).list_accounts() == []


def test_list_accounts_passes_an_explicit_json_flag(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(_LIST_PAYLOAD))
    )

    _cswap(tmp_path).list_accounts()

    assert run.call_args.args[0] == ["cswap", "list", "--json"]


# --- list: malformed rows -----------------------------------------------


def test_a_row_missing_number_is_a_cswap_error(tmp_path, mocker):
    payload = {"accounts": [{"email": "work@gisgro.com", "organizationUuid": "org-abc"}]}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "number" in str(exc.value)


def test_a_row_missing_email_is_a_cswap_error(tmp_path, mocker):
    """A row silently defaulted to email="" would read back as a
    legitimate-looking account with an empty pool identity."""
    payload = {"accounts": [{"number": 1, "organizationUuid": "org-abc"}]}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "email" in str(exc.value)


def test_a_row_with_no_organization_and_no_alias_still_parses(tmp_path, mocker):
    """Guard against over-correction: an empty organizationUuid is a
    legitimate personal account, not a malformed row."""
    payload = {
        "accounts": [
            {
                "number": 1,
                "email": "me@example.com",
                "organizationUuid": "",
                "active": False,
                "usageStatus": "ok",
            }
        ]
    }
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    (account,) = _cswap(tmp_path).list_accounts()

    assert account.identity == ("me@example.com", "")
    assert account.alias == ""
    assert account.label == "me@example.com"


def test_a_row_with_a_non_integer_number_is_a_cswap_error(tmp_path, mocker):
    payload = {"accounts": [{"number": "one", "email": "me@example.com"}]}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "number" in str(exc.value)


# --- status ------------------------------------------------------------


def test_status_reports_no_login(tmp_path, mocker):
    payload = {"schemaVersion": 1, "active": None}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    live = _cswap(tmp_path).status()

    assert live.email is None and live.managed is False
    assert live.identity is None


def test_status_reports_an_unpooled_login_with_its_email(tmp_path, mocker):
    payload = {"schemaVersion": 1, "active": {"email": "stray@example.com", "managed": False}}
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    live = _cswap(tmp_path).status()

    assert live.email == "stray@example.com"
    assert live.managed is False
    assert live.number is None
    assert live.identity is None, "an unpooled login has no pool identity"


def test_status_reports_a_pooled_login(tmp_path, mocker):
    payload = {
        "schemaVersion": 1,
        "active": {
            "number": 2,
            "email": "me@example.com",
            "organizationUuid": "",
            "managed": True,
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 1.0}},
        },
        "totalManagedAccounts": 2,
    }
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    live = _cswap(tmp_path).status()

    assert live.managed is True
    assert live.number == 2
    assert live.identity == ("me@example.com", "")


# --- switch ------------------------------------------------------------


def test_switch_always_passes_an_explicit_target(tmp_path, mocker):
    """A bare `cswap switch` rotates from the STORED activeAccountNumber, which
    is meaningless when several repos share one store."""
    payload = {
        "schemaVersion": 1,
        "switched": True,
        "from": {"number": 1, "email": "work@gisgro.com"},
        "to": {"number": 2, "email": "me@example.com"},
        "strategy": "direct",
        "reason": "switched",
        "message": "Switched to Account-2 (me@example.com)",
        "warnings": [],
    }
    run = mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    message = _cswap(tmp_path).switch("2")

    assert run.call_args.args[0] == ["cswap", "switch", "2", "--json"]
    assert message == "Switched to Account-2 (me@example.com)"


def test_switch_surfaces_cswap_warnings_in_the_message(tmp_path, mocker):
    payload = {
        "schemaVersion": 1,
        "switched": True,
        "from": None,
        "to": {"number": 2, "email": "me@example.com"},
        "strategy": "direct",
        "reason": "switched",
        "message": "Switched to Account-2 (me@example.com)",
        "warnings": ["Account-2 has a live session-mode Claude instance"],
    }
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(json.dumps(payload)))

    message = _cswap(tmp_path).switch("2")

    assert "live session-mode" in message


# --- failure modes -----------------------------------------------------


def test_a_json_error_envelope_becomes_a_cswap_error(tmp_path, mocker):
    """`--json` puts the handled-error message on STDOUT, not stderr."""
    envelope = {
        "schemaVersion": 1,
        "error": {"type": "AccountNotFoundError", "message": "No account found: 7"},
    }
    mocker.patch(
        "jailbee.cswap.subprocess.run",
        return_value=_completed(json.dumps(envelope), stderr="", code=1),
    )

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).switch("7")

    assert "No account found: 7" in str(exc.value)


def test_a_non_json_failure_preserves_stderr(tmp_path, mocker):
    mocker.patch(
        "jailbee.cswap.subprocess.run",
        return_value=_completed("", stderr="Error: keychain locked", code=1),
    )

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "keychain locked" in str(exc.value)


def test_unparseable_stdout_on_success_is_an_error_not_a_crash(tmp_path, mocker):
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed("not json"))

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "JSON" in str(exc.value)


def test_a_missing_binary_is_a_cswap_missing_error(tmp_path, mocker):
    mocker.patch("jailbee.cswap.subprocess.run", side_effect=FileNotFoundError())

    with pytest.raises(CswapMissingError) as exc:
        _cswap(tmp_path).list_accounts()

    assert "claude-swap" in str(exc.value), "the error carries the install hint"


def test_a_timeout_is_a_cswap_timeout_error(tmp_path, mocker):
    mocker.patch(
        "jailbee.cswap.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["cswap"], timeout=120),
    )

    with pytest.raises(CswapTimeoutError):
        _cswap(tmp_path).list_accounts()


def test_available_is_false_when_the_binary_is_absent(tmp_path, mocker):
    mocker.patch("jailbee.cswap.shutil.which", return_value=None)

    assert _cswap(tmp_path).available() is False


def test_available_is_true_when_the_binary_is_on_path(tmp_path, mocker):
    mocker.patch("jailbee.cswap.shutil.which", return_value="/usr/bin/cswap")

    assert _cswap(tmp_path).available() is True


def test_the_conftest_fixture_hides_a_real_cswap_on_path(tmp_path, monkeypatch):
    """Pin `_neutralize_cswap_autodetect` (tests/conftest.py) so it cannot be
    deleted or narrowed silently: this test plants a genuinely executable
    `cswap` (and `claude-swap`) on `PATH` itself, so it fails without the
    fixture instead of merely trusting the host has neither installed.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    cswap_exe = bindir / CSWAP_BINARY
    claude_swap_exe = bindir / "claude-swap"
    probe_exe = bindir / "jailbee-fake-probe"
    for exe in (cswap_exe, claude_swap_exe, probe_exe):
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    # The fixture's cswap-shaped hole: both aliases vanish even though they
    # are genuinely on PATH right now.
    assert shutil.which(CSWAP_BINARY) is None
    assert shutil.which("claude-swap") is None
    # The delegation still answers truthfully for a name the fixture does
    # not cover — asserted against a binary this test created itself, not a
    # bet on what the host happens to have installed.
    assert shutil.which("jailbee-fake-probe") == str(probe_exe)
    # Pinned through the public method the production code actually calls.
    assert _cswap(tmp_path).available() is False


# --- version -------------------------------------------------------------


def test_version_returns_stripped_stdout(tmp_path, mocker):
    home = tmp_path / "shared" / "claude"
    run = mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed("cswap 0.26.0b1\n"))

    version = Cswap(config_home=home).version()

    assert run.call_args.args[0] == ["cswap", "--version"]
    assert version == "cswap 0.26.0b1"
    env = run.call_args.kwargs["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(home)


def test_version_failure_preserves_detail(tmp_path, mocker):
    mocker.patch(
        "jailbee.cswap.subprocess.run",
        return_value=_completed("", stderr="Error: corrupt install", code=1),
    )

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).version()

    assert "corrupt install" in str(exc.value)


def test_version_missing_binary_is_a_cswap_missing_error(tmp_path, mocker):
    mocker.patch("jailbee.cswap.subprocess.run", side_effect=FileNotFoundError())

    with pytest.raises(CswapMissingError) as exc:
        _cswap(tmp_path).version()

    assert "claude-swap" in str(exc.value)


def test_version_timeout_is_a_cswap_timeout_error(tmp_path, mocker):
    mocker.patch(
        "jailbee.cswap.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["cswap", "--version"], timeout=30),
    )

    with pytest.raises(CswapTimeoutError):
        _cswap(tmp_path).version()


# --- interactive passthrough -------------------------------------------


def test_add_runs_interactively_without_capturing(tmp_path, mocker):
    """`cswap add` prompts on an occupied slot and has no --yes flag, so its
    stdin must be the user's terminal, not DEVNULL."""
    run = mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(code=0))

    _cswap(tmp_path).add(alias="work", slot=3)

    assert run.call_args.args[0] == ["cswap", "add", "--alias", "work", "--slot", "3"]
    kwargs = run.call_args.kwargs
    assert "capture_output" not in kwargs or kwargs["capture_output"] is False
    assert "stdin" not in kwargs, "stdin is inherited, never DEVNULL"


def test_add_omits_flags_that_were_not_given(tmp_path, mocker):
    run = mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(code=0))

    _cswap(tmp_path).add(alias=None, slot=None)

    assert run.call_args.args[0] == ["cswap", "add"]


def test_remove_runs_interactively(tmp_path, mocker):
    """`cswap remove` prompts [y/N] with no --yes flag."""
    run = mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(code=0))

    _cswap(tmp_path).remove("2")

    assert run.call_args.args[0] == ["cswap", "remove", "2"]
    assert "stdin" not in run.call_args.kwargs


def test_a_failed_interactive_run_raises_without_inventing_detail(tmp_path, mocker):
    mocker.patch("jailbee.cswap.subprocess.run", return_value=_completed(code=1))

    with pytest.raises(CswapError) as exc:
        _cswap(tmp_path).add(alias=None, slot=None)

    assert "exit 1" in str(exc.value)


# --- config_home -------------------------------------------------------


def test_config_home_is_the_claude_subdir_of_shared_dir(tmp_path):
    from jailbee.cswap import config_home
    from tests.conftest import make_config

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_config(repo, shared_dir=tmp_path / "shared")

    assert config_home(cfg) == tmp_path / "shared" / "claude"
