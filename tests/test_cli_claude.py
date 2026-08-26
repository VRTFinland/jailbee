"""Tests for the `jailbee claude` command group."""

from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.cswap import Account

runner = CliRunner()

WORK = Account(
    number=1,
    email="work@gisgro.com",
    org_uuid="org-abc",
    org_name="Gisgro",
    alias="work",
    active=True,
    disabled=False,
    usage_status="ok",
    five_hour_pct=34.0,
    seven_day_pct=61.0,
)
PERSONAL = Account(
    number=2,
    email="me@example.com",
    org_uuid="",
    org_name="",
    alias="personal",
    active=False,
    disabled=False,
    usage_status="ok",
    five_hour_pct=0.0,
    seven_day_pct=12.0,
)


def _repo(tmp_path, mocker, *, available=True, accounts=None, live=None):
    """Wire a repo config and a fake Cswap. Returns (cfg, fake_cswap).

    `agents={"claude": {"enabled": True}}` is required: with no override the
    default config has `cfg.claude.enabled is False`, and `_claude_ctx`'s
    enabled-guard would exit 1 before ever reaching cswap.
    """
    from jailbee.cswap import LiveAccount
    from tests.conftest import make_config

    repo_root = tmp_path / "gisgro"
    repo_root.mkdir()
    cfg_dir = repo_root / ".jailbee"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({}))
    cfg = make_config(
        repo_root, shared_dir=tmp_path / "shared", agents={"claude": {"enabled": True}}
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path", return_value=cfg_dir / "config.yaml")

    fake = mocker.MagicMock()
    fake.available.return_value = available
    fake.list_accounts.return_value = list(accounts if accounts is not None else [WORK, PERSONAL])
    fake.status.return_value = live or LiveAccount(
        email="work@gisgro.com", managed=True, number=1, org_uuid="org-abc"
    )
    fake.switch.return_value = "Switched to Account-2 (me@example.com)"
    mocker.patch("jailbee.cswap.Cswap", return_value=fake)
    return cfg, fake


# --- the no-cswap path is first class ---------------------------------


def test_ls_without_cswap_prints_the_install_hint_and_exits(tmp_path, mocker):
    _repo(tmp_path, mocker, available=False)

    result = runner.invoke(app, ["claude", "ls"])

    assert result.exit_code == 1
    assert "claude-swap" in result.output
    assert "uv tool install" in result.output


def test_use_without_cswap_prints_the_install_hint_and_exits(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker, available=False)

    result = runner.invoke(app, ["claude", "use", "2"])

    assert result.exit_code == 1
    assert "claude-swap" in result.output
    fake.switch.assert_not_called()


def test_a_bare_claude_shows_the_verbs(tmp_path, mocker):
    result = runner.invoke(app, ["claude"])

    assert result.exit_code != 0, "no_args_is_help exits non-zero, like every group"
    for verb in ("ls", "use", "add", "allow", "release", "rm"):
        assert verb in result.output


# --- ls ---------------------------------------------------------------


def test_ls_renders_slot_alias_quota_and_holder(tmp_path, mocker):
    _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "250"})

    assert result.exit_code == 0
    assert "work" in result.output
    assert "34" in result.output and "61" in result.output
    assert "me@example.com" in result.output


def test_ls_marks_this_repos_holding(tmp_path, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import ClaudeAccountHolding

    _repo(tmp_path, mocker)
    with Session(get_engine()) as s:
        s.add(
            ClaudeAccountHolding(
                email=WORK.email,
                org_uuid=WORK.org_uuid,
                container_prefix="gisgro",
                slot="1",
                state="held",
                since=datetime(2026, 8, 26, tzinfo=UTC),
            )
        )
        s.commit()

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "250"})

    assert "gisgro" in result.output
    assert "<-" in result.output


def test_ls_json_is_pure_json(tmp_path, mocker):
    _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "ls", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [row["slot"] for row in payload] == [1, 2]
    assert payload[0]["alias"] == "work"
    assert payload[0]["five_hour_pct"] == 34.0
    assert payload[0]["holder"] is None


def test_ls_reports_an_empty_pool_with_the_next_step(tmp_path, mocker):
    _repo(tmp_path, mocker, accounts=[])

    result = runner.invoke(app, ["claude", "ls"])

    assert result.exit_code == 0
    assert "jailbee claude add" in result.output


def test_ls_reports_a_cswap_failure_with_what_cswap_said(tmp_path, mocker):
    from jailbee.cswap import CswapError

    _, fake = _repo(tmp_path, mocker)
    fake.list_accounts.side_effect = CswapError("credential store is locked")

    result = runner.invoke(app, ["claude", "ls"])

    assert result.exit_code == 1
    assert "credential store is locked" in result.output


def test_ls_says_which_account_this_repo_is_on(tmp_path, mocker):
    _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "250"})

    assert "gisgro is on account 1 (work)" in result.output


# --- use --------------------------------------------------------------


def test_use_switches_and_reports_what_cswap_said(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "use", "2"])

    assert result.exit_code == 0
    fake.switch.assert_called_once_with("2")
    assert "Switched to Account-2" in result.output


def test_use_refuses_an_unsaved_live_login_and_names_the_fix(tmp_path, mocker):
    from jailbee.cswap import LiveAccount

    _, fake = _repo(
        tmp_path,
        mocker,
        live=LiveAccount(email="stray@example.com", managed=False, number=None, org_uuid=""),
    )

    result = runner.invoke(app, ["claude", "use", "2"])

    assert result.exit_code == 1
    assert "stray@example.com" in result.output
    assert "jailbee claude add" in result.output
    fake.switch.assert_not_called()


def test_use_force_switches_over_an_unsaved_login(tmp_path, mocker):
    from jailbee.cswap import LiveAccount

    _, fake = _repo(
        tmp_path,
        mocker,
        live=LiveAccount(email="stray@example.com", managed=False, number=None, org_uuid=""),
    )
    mocker.patch("jailbee.tui.default_confirm", return_value=True)

    result = runner.invoke(app, ["claude", "use", "2", "--force"])

    assert result.exit_code == 0
    fake.switch.assert_called_once_with("2")


def test_use_force_prompts_and_a_no_aborts(tmp_path, mocker):
    from jailbee.cswap import LiveAccount

    _, fake = _repo(
        tmp_path,
        mocker,
        live=LiveAccount(email="stray@example.com", managed=False, number=None, org_uuid=""),
    )
    mocker.patch("jailbee.tui.default_confirm", return_value=False)

    result = runner.invoke(app, ["claude", "use", "2", "--force"])

    assert result.exit_code == 1
    fake.switch.assert_not_called()
    assert "stray@example.com" in result.output, "the prompt states what is at stake"


def test_use_without_an_argument_and_without_a_tty_fails_instead_of_hanging(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    pick = mocker.patch("jailbee.tui.pick_claude_account")

    result = runner.invoke(app, ["claude", "use"])

    assert result.exit_code == 2
    pick.assert_not_called()
    fake.switch.assert_not_called()


def test_use_without_an_argument_opens_the_picker_on_a_tty(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_claude_account", return_value="2")

    result = runner.invoke(app, ["claude", "use"])

    assert result.exit_code == 0
    fake.switch.assert_called_once_with("2")


def test_a_cancelled_picker_switches_nothing(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_claude_account", return_value=None)

    result = runner.invoke(app, ["claude", "use"])

    assert result.exit_code == 0
    fake.switch.assert_not_called()


# --- add --------------------------------------------------------------


def test_add_captures_the_current_login_with_an_explicit_alias(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "add", "--alias", "work"])

    assert result.exit_code == 0
    fake.add.assert_called_once_with(alias="work", slot=None)


def test_add_passes_a_slot_through(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "add", "--alias", "work", "--slot", "3"])

    assert result.exit_code == 0
    fake.add.assert_called_once_with(alias="work", slot=3)


def test_add_without_an_alias_defaults_to_the_emails_local_part(tmp_path, mocker):
    from jailbee.cswap import LiveAccount

    _, fake = _repo(
        tmp_path,
        mocker,
        live=LiveAccount(email="tuomas@gisgro.com", managed=False, number=None, org_uuid=""),
    )
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    prompt = mocker.patch("jailbee.cli.typer.prompt", return_value="tuomas")

    result = runner.invoke(app, ["claude", "add"])

    assert result.exit_code == 0
    assert prompt.call_args.kwargs["default"] == "tuomas"
    fake.add.assert_called_once_with(alias="tuomas", slot=None)


def test_add_without_an_alias_and_without_a_tty_fails(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=False)

    result = runner.invoke(app, ["claude", "add"])

    assert result.exit_code == 2
    fake.add.assert_not_called()


def test_add_refuses_when_there_is_no_live_login_to_capture(tmp_path, mocker):
    from jailbee.cswap import LiveAccount

    _, fake = _repo(
        tmp_path, mocker, live=LiveAccount(email=None, managed=False, number=None, org_uuid="")
    )

    result = runner.invoke(app, ["claude", "add", "--alias", "work"])

    assert result.exit_code == 1
    assert "not logged in" in result.output.lower()
    fake.add.assert_not_called()


# --- allow ------------------------------------------------------------


def test_allow_with_arguments_replaces_the_list(tmp_path, mocker):
    from sqlmodel import Session

    from jailbee import claude_accounts
    from jailbee.db import get_engine

    _repo(tmp_path, mocker)
    runner.invoke(app, ["claude", "allow", "1", "2"])

    result = runner.invoke(app, ["claude", "allow", "personal"])

    assert result.exit_code == 0
    with Session(get_engine()) as s:
        assert claude_accounts.allowed_identities(s, "gisgro") == {PERSONAL.identity}


def test_allow_rejects_an_unknown_reference_and_stores_nothing(tmp_path, mocker):
    from sqlmodel import Session

    from jailbee import claude_accounts
    from jailbee.db import get_engine

    _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "allow", "1", "nope"])

    assert result.exit_code == 1
    with Session(get_engine()) as s:
        assert claude_accounts.allowed_identities(s, "gisgro") == set(), "all or nothing"


def test_allow_without_arguments_and_without_a_tty_fails(tmp_path, mocker):
    _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    pick = mocker.patch("jailbee.tui.pick_claude_accounts_multi")

    result = runner.invoke(app, ["claude", "allow"])

    assert result.exit_code == 2
    pick.assert_not_called()


def test_allow_with_nothing_checked_clears_the_restriction(tmp_path, mocker):
    from sqlmodel import Session

    from jailbee import claude_accounts
    from jailbee.db import get_engine

    _repo(tmp_path, mocker)
    runner.invoke(app, ["claude", "allow", "1"])
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_claude_accounts_multi", return_value=[])

    result = runner.invoke(app, ["claude", "allow"])

    assert result.exit_code == 0
    with Session(get_engine()) as s:
        assert claude_accounts.allowed_identities(s, "gisgro") == set()
    assert "every account" in result.output


# --- release ----------------------------------------------------------


def test_release_frees_this_repos_holding(tmp_path, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import ClaudeAccountHolding

    _repo(tmp_path, mocker)
    with Session(get_engine()) as s:
        s.add(
            ClaudeAccountHolding(
                email=WORK.email,
                org_uuid=WORK.org_uuid,
                container_prefix="gisgro",
                slot="1",
                state="held",
                since=datetime(2026, 8, 26, tzinfo=UTC),
            )
        )
        s.commit()

    result = runner.invoke(app, ["claude", "release"])

    assert result.exit_code == 0
    with Session(get_engine()) as s:
        assert s.get(ClaudeAccountHolding, WORK.identity) is None


def test_release_with_nothing_held_says_so_and_succeeds(tmp_path, mocker):
    _repo(tmp_path, mocker)

    result = runner.invoke(app, ["claude", "release"])

    assert result.exit_code == 0
    assert "holds no account" in result.output


def test_release_with_a_ref_frees_another_repos_holding(tmp_path, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import ClaudeAccountHolding

    _repo(tmp_path, mocker)
    with Session(get_engine()) as s:
        s.add(
            ClaudeAccountHolding(
                email=PERSONAL.email,
                org_uuid=PERSONAL.org_uuid,
                container_prefix="goneaway",
                slot="2",
                state="claiming",
                since=datetime(2026, 8, 26, tzinfo=UTC),
            )
        )
        s.commit()

    result = runner.invoke(app, ["claude", "release", "2"])

    assert result.exit_code == 0
    assert "goneaway" in result.output
    with Session(get_engine()) as s:
        assert s.get(ClaudeAccountHolding, PERSONAL.identity) is None


# --- rm ---------------------------------------------------------------


def test_rm_removes_from_the_pool_and_the_ledger(tmp_path, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import ClaudeAccountHolding

    _, fake = _repo(tmp_path, mocker)
    with Session(get_engine()) as s:
        s.add(
            ClaudeAccountHolding(
                email=PERSONAL.email,
                org_uuid=PERSONAL.org_uuid,
                container_prefix="gisgro",
                slot="2",
                state="held",
                since=datetime(2026, 8, 26, tzinfo=UTC),
            )
        )
        s.commit()

    result = runner.invoke(app, ["claude", "rm", "personal"])

    assert result.exit_code == 0
    fake.remove.assert_called_once_with("2")
    with Session(get_engine()) as s:
        assert s.get(ClaudeAccountHolding, PERSONAL.identity) is None


def test_rm_without_an_argument_and_without_a_tty_fails(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=False)

    result = runner.invoke(app, ["claude", "rm"])

    assert result.exit_code == 2
    fake.remove.assert_not_called()


def test_rm_without_an_argument_opens_the_picker_on_a_tty(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_claude_account", return_value="2")

    result = runner.invoke(app, ["claude", "rm"])

    assert result.exit_code == 0
    fake.remove.assert_called_once_with("2")
