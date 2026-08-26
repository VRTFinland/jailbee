"""Tests for the `jailbee claude` command group."""

from __future__ import annotations

import json

import pytest
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


@pytest.mark.xfail(reason="`claude use` lands in Task 8", strict=True)
def test_use_without_cswap_prints_the_install_hint_and_exits(tmp_path, mocker):
    _, fake = _repo(tmp_path, mocker, available=False)

    result = runner.invoke(app, ["claude", "use", "2"])

    assert result.exit_code == 1
    assert "claude-swap" in result.output
    fake.switch.assert_not_called()


@pytest.mark.xfail(reason="`use`/`add`/`allow`/`release`/`rm` land in Task 8", strict=True)
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
