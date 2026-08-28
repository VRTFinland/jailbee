"""jailbee's own Claude account pool."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from jailbee import claude_pool
from jailbee.claude_pool import Identity, Slot
from tests.conftest import make_cfg


def _cfg(tmp_path: Path, *, group: str | None = None):
    """A Config whose shared_dir is under tmp_path, never under $HOME.

    `claude_credentials_dir` is an ordinary Config field, so it is passed as
    an override — the same form `tests/test_doctor.py:1678` uses.
    """
    extra = (
        {"claude_credentials_dir": tmp_path / "creds" / group} if group is not None else {}
    )
    return make_cfg(tmp_path / "repo", shared_dir=tmp_path / "shared", **extra)


def test_store_dir_follows_xdg_data_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert claude_pool.store_dir() == (
        tmp_path / "data" / "jailbee" / "claude-credentials" / "_parked"
    )


def test_holder_is_the_config_home_without_a_group(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert claude_pool.config_home(cfg) == tmp_path / "shared" / "claude"
    assert claude_pool.holder_dir(cfg) == tmp_path / "shared" / "claude"
    assert (
        claude_pool.live_credential_path(cfg)
        == tmp_path / "shared" / "claude" / ".credentials.json"
    )


def test_holder_is_the_group_directory_with_a_group(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, group="work")
    # The config home does NOT move: only the credential is shared.
    assert claude_pool.config_home(cfg) == tmp_path / "shared" / "claude"
    assert claude_pool.holder_dir(cfg) == tmp_path / "creds" / "work"


def test_identity_file_prefers_the_legacy_config_json(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    assert claude_pool.identity_file(home) == home / ".claude.json"
    (home / ".config.json").write_text("{}", encoding="utf-8")
    assert claude_pool.identity_file(home) == home / ".config.json"


def _write_identity(home: Path, block: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": block, "projects": {"/x": {}}}), encoding="utf-8"
    )


def test_read_identity_returns_email_and_org(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    _write_identity(
        home,
        {
            "emailAddress": "Me@Corp.COM",
            "accountUuid": "aaaa",
            "organizationUuid": "A1B2C3D4-5678",
        },
    )
    assert claude_pool.read_identity(home) == Identity(
        email="Me@Corp.COM", org_uuid="A1B2C3D4-5678"
    )


def test_read_identity_tolerates_a_missing_organization(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    _write_identity(home, {"emailAddress": "me@example.com"})
    assert claude_pool.read_identity(home) == Identity(email="me@example.com")


@pytest.mark.parametrize(
    "block", [None, {}, {"emailAddress": ""}, {"emailAddress": 7}, "nonsense"]
)
def test_read_identity_is_none_when_unusable(tmp_path: Path, block: object) -> None:
    home = tmp_path / "claude"
    _write_identity(home, block)
    assert claude_pool.read_identity(home) is None


def test_read_identity_is_none_on_a_missing_or_torn_file(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    assert claude_pool.read_identity(home) is None
    home.mkdir()
    (home / ".claude.json").write_text('{"oauthAccount": {"emai', encoding="utf-8")
    assert claude_pool.read_identity(home) is None


def test_slug_lowercases_and_truncates_the_org() -> None:
    slug = claude_pool.slug_for(Identity("Me@Corp.COM", "A1B2C3D4-5678-90ab"))
    assert slug == "me@corp.com#a1b2c3d4"


def test_slug_omits_the_org_when_absent() -> None:
    assert claude_pool.slug_for(Identity("me@example.com")) == "me@example.com"


def test_slug_replaces_characters_a_filename_should_not_carry() -> None:
    assert claude_pool.slug_for(Identity("a b/c@x.com")) == "a-b-c@x.com"


def test_unknown_slot_name_is_sortable() -> None:
    name = claude_pool.unknown_slot_name(datetime(2026, 8, 28, 10, 42, 33))
    assert name == "unknown-20260828-104233"


def test_slot_email_and_org_hint() -> None:
    slot = Slot(name="me@corp.com#a1b2c3d4", path=Path("/x"), live=False)
    assert slot.email == "me@corp.com"
    assert slot.org_hint == "a1b2c3d4"

    plain = Slot(name="me@example.com", path=Path("/x"), live=False)
    assert plain.email == "me@example.com"
    assert plain.org_hint is None

    unknown = Slot(name="unknown-20260828-104233", path=Path("/x"), live=False)
    assert unknown.email is None
    assert unknown.org_hint is None

    live = Slot(name=claude_pool.LIVE_UNIDENTIFIED, path=Path("/x"), live=True)
    assert live.email is None
