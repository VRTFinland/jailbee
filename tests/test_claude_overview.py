"""The host-wide view behind `jailbee claude ls`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jailbee import claude_groups, claude_overview, claude_pool
from jailbee.global_config import GlobalConfig
from tests.conftest import make_cfg

ACCOUNT_BLOCK = {
    "emailAddress": "work@corp.com",
    "accountUuid": "1111-2222",
    "organizationUuid": "ccccdddd-1111",
    "organizationName": "First Org",
}


def _grant(token: str) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": token}})


def _no_git(mocker) -> None:
    """Keep `load_config` off the `git` binary, as tests/test_claude_pool.py does."""
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")


def _register(prefix: str, repo_root: Path) -> None:
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=str(repo_root),
                registered_at=datetime(2026, 9, 5, tzinfo=UTC),
            )
        )
        session.commit()


def _write_repo(root: Path, *, shared: Path) -> None:
    (root / ".jailbee").mkdir(parents=True)
    (root / ".jailbee" / "config.yaml").write_text(
        f"container_prefix: {root.name}\nshared_dir: {shared}\n", encoding="utf-8"
    )


def _cfg(tmp_path: Path, *, group: str | None = None):
    extra = {"claude_credentials_dir": claude_groups.group_dir(group)} if group else {}
    return make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared", **extra)


def _gcfg(**creds) -> GlobalConfig:
    return GlobalConfig.model_validate({"claude_credentials": creds} if creds else {})


def _login_in(holder: Path, token: str, *, account: dict | None = None) -> None:
    """A holder directory with a live credential, optionally named by its note."""
    holder.mkdir(parents=True, exist_ok=True)
    (holder / claude_pool.CREDENTIAL_FILE).write_text(_grant(token), encoding="utf-8")
    if account is not None:
        claude_pool.write_account_note(holder, account, _grant(token))


def _identity_in(home: Path, block: dict) -> None:
    """A repo's config home, as Claude Code leaves it after a run."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": block}), encoding="utf-8")


def _raw(name: str, group: str | None = None) -> dict:
    config = {} if group is None else {claude_groups.GROUP_LABEL: group}
    return {"name": name, "status": "Running", "profiles": [], "config": config, "state": None}


def _incus(mocker, rows: list[dict] | None = None):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = rows or []
    return incus


def _row(overview: claude_overview.Overview, group: str | None, prefix: str | None = None):
    """The one row for a holder, or None. Parked rows are addressed by name."""
    found = [r for r in overview.rows if r.group == group and r.prefix == prefix]
    assert len(found) <= 1, f"more than one row for {group!r}/{prefix!r}"
    return found[0] if found else None


@pytest.fixture(autouse=True)
def _xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def test_a_group_row_names_the_login_from_the_holder_note(tmp_path: Path, mocker) -> None:
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work", account=ACCOUNT_BLOCK)

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    row = _row(overview, "work")
    assert row is not None
    assert row.state == "live"
    # `display_name` drops the org: the ORG column carries that half, and
    # repeating it in both is what `cli._claude_fields` already avoids.
    assert row.account == "work@corp.com"
    assert row.org_hint == "ccccdddd"


def test_a_group_row_falls_back_to_an_authoritative_member(tmp_path: Path, mocker) -> None:
    """Holders predating the account note are named by a member repo's
    `oauthAccount` — the same rule `park` uses."""
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work")
    _identity_in(claude_pool.config_home(cfg), ACCOUNT_BLOCK)

    overview = claude_overview.build(
        cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker, [_raw("myrepo-a")])
    )

    row = _row(overview, "work")
    assert row is not None
    assert row.account == "work@corp.com"


def test_a_group_row_is_unidentified_when_nothing_names_the_login(
    tmp_path: Path, mocker
) -> None:
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work")

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    row = _row(overview, "work")
    assert row is not None
    assert row.state == "live"
    assert row.account == claude_pool.LIVE_UNIDENTIFIED


def test_a_group_with_no_login_is_still_a_row(tmp_path: Path, mocker) -> None:
    """A group created but never filled was invisible before: `claude group`
    printed its name and nothing said it held no login."""
    cfg = _cfg(tmp_path)
    claude_groups.group_dir("fresh").mkdir(parents=True)

    overview = claude_overview.build(cfg, _gcfg(), _incus(mocker))

    row = _row(overview, "fresh")
    assert row is not None
    assert row.state == "empty"
    assert row.account is None


def test_a_group_only_a_container_uses_lists_that_container(tmp_path: Path, mocker) -> None:
    """The temporary-override case: no repo resolves to `personal`, so nothing
    but the container label says the group is in use at all."""
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("personal"), "rt-personal", account=ACCOUNT_BLOCK)
    rows = [_raw("myrepo-a"), _raw("myrepo-b", "personal")]

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker, rows))

    row = _row(overview, "personal")
    assert row is not None
    assert row.repos == ()
    assert row.containers == ("myrepo-b",)
    assert _row(overview, "work").containers == ("myrepo-a",)


def test_a_group_named_only_by_a_container_label_is_a_row(tmp_path: Path, mocker) -> None:
    """A label whose directory was removed by hand is a broken state worth
    showing, not one to hide."""
    cfg = _cfg(tmp_path)

    overview = claude_overview.build(
        cfg, _gcfg(), _incus(mocker, [_raw("myrepo-a", "vanished")])
    )

    row = _row(overview, "vanished")
    assert row is not None
    assert row.state == "empty"
    assert row.containers == ("myrepo-a",)


def test_the_repos_own_holder_is_marked_mine(tmp_path: Path, mocker) -> None:
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work", account=ACCOUNT_BLOCK)
    _login_in(claude_groups.group_dir("other"), "rt-other")

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    assert _row(overview, "work").mine is True
    assert _row(overview, "other").mine is False


def test_a_group_row_lists_every_repo_resolving_to_it(tmp_path: Path, mocker) -> None:
    _no_git(mocker)
    cfg = _cfg(tmp_path, group="work")
    other = tmp_path / "other"
    _write_repo(other, shared=tmp_path / "other-shared")
    _register("other", other)
    _login_in(claude_groups.group_dir("work"), "rt-work", account=ACCOUNT_BLOCK)

    overview = claude_overview.build(
        cfg, _gcfg(group="work"), _incus(mocker, [_raw("myrepo-a"), _raw("other-a")])
    )

    row = _row(overview, "work")
    assert row.repos == ("myrepo", "other")
    assert row.containers == ("myrepo-a", "other-a")


def test_an_ungrouped_repo_holder_is_its_own_row(tmp_path: Path, mocker) -> None:
    """`no group` is not one shared holder: each such repo keeps its own login,
    so each is a row of its own, named by the repo."""
    _no_git(mocker)
    cfg = _cfg(tmp_path)
    other = tmp_path / "other"
    _write_repo(other, shared=tmp_path / "other-shared")
    _register("other", other)
    _login_in(tmp_path / "other-shared" / "claude", "rt-other", account=ACCOUNT_BLOCK)

    overview = claude_overview.build(cfg, _gcfg(), _incus(mocker, [_raw("other-a")]))

    row = _row(overview, None, "other")
    assert row is not None
    assert row.state == "live"
    assert row.account == "work@corp.com"
    assert row.containers == ("other-a",)


def test_an_ungrouped_repo_with_no_login_is_not_a_row(tmp_path: Path, mocker) -> None:
    """One empty row per registered repo would bury the table; an empty *group*
    is worth showing because someone created it on purpose."""
    _no_git(mocker)
    cfg = _cfg(tmp_path, group="work")
    other = tmp_path / "other"
    _write_repo(other, shared=tmp_path / "other-shared")
    _register("other", other)

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    assert _row(overview, None, "other") is None


def test_the_calling_repos_empty_holder_is_always_a_row(tmp_path: Path, mocker) -> None:
    """The table has to say what *this* repo uses, even when nothing is logged in."""
    cfg = _cfg(tmp_path)

    overview = claude_overview.build(cfg, _gcfg(), _incus(mocker))

    row = _row(overview, None, "myrepo")
    assert row is not None
    assert row.state == "empty"
    assert row.mine is True


def test_an_ungrouped_repo_spanning_a_group_does_not_name_its_own_holder(
    tmp_path: Path, mocker
) -> None:
    """One container moved into a group makes the repo's `~/.claude` describe
    whichever account ran last — so it can no longer name its own login."""
    cfg = _cfg(tmp_path)
    _login_in(claude_pool.config_home(cfg), "rt-mine")
    _identity_in(claude_pool.config_home(cfg), ACCOUNT_BLOCK)
    rows = [_raw("myrepo-a"), _raw("myrepo-b", "personal")]

    overview = claude_overview.build(cfg, _gcfg(), _incus(mocker, rows))

    row = _row(overview, None, "myrepo")
    assert row.account == claude_pool.LIVE_UNIDENTIFIED


def test_parked_logins_are_rows_of_their_own(tmp_path: Path, mocker) -> None:
    cfg = _cfg(tmp_path)
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    (store / "parked@corp.com.json").write_text(_grant("rt-parked"), encoding="utf-8")

    overview = claude_overview.build(cfg, _gcfg(), _incus(mocker))

    parked = [r for r in overview.rows if r.state == "parked"]
    assert [r.account for r in parked] == ["parked@corp.com"]
    assert parked[0].group is None
    assert parked[0].containers == ()


def test_a_live_row_colliding_with_a_parked_name_takes_the_live_suffix(
    tmp_path: Path, mocker
) -> None:
    """`park` then `/login` as the same account is the documented way to hold two
    grants; the ref a script reads back has to stay the one `claude use` knows."""
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-live", account=ACCOUNT_BLOCK)
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    (store / "work@corp.com#ccccdddd.json").write_text(_grant("rt-parked"), encoding="utf-8")

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    assert _row(overview, "work").name == "work@corp.com#ccccdddd~live"


def test_containers_are_unknown_when_incus_cannot_be_listed(tmp_path: Path, mocker) -> None:
    """A listing must still list: an unreachable daemon costs the container
    column, not the command."""
    from jailbee.incus import IncusError

    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work", account=ACCOUNT_BLOCK)
    incus = mocker.MagicMock()
    incus.list_containers.side_effect = IncusError("connection refused")

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), incus)

    assert overview.containers_known is False
    assert _row(overview, "work").account == "work@corp.com"


def test_a_member_repo_whose_config_will_not_load_is_reported(tmp_path: Path, mocker) -> None:
    cfg = _cfg(tmp_path, group="work")
    broken = tmp_path / "broken"
    (broken / ".jailbee").mkdir(parents=True)
    (broken / ".jailbee" / "config.yaml").write_text(": not yaml :", encoding="utf-8")
    _register("broken", broken)

    overview = claude_overview.build(cfg, _gcfg(group="work"), _incus(mocker))

    assert overview.unreachable == ("broken",)


def test_rows_are_ordered_live_then_empty_then_parked(tmp_path: Path, mocker) -> None:
    cfg = _cfg(tmp_path, group="work")
    _login_in(claude_groups.group_dir("work"), "rt-work", account=ACCOUNT_BLOCK)
    claude_groups.group_dir("zzz-empty").mkdir(parents=True)
    _login_in(claude_groups.group_dir("alpha"), "rt-alpha")
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    (store / "parked@corp.com.json").write_text(_grant("rt-parked"), encoding="utf-8")

    overview = claude_overview.build(cfg, _gcfg(repos={"myrepo": "work"}), _incus(mocker))

    assert [(r.state, r.group) for r in overview.rows] == [
        ("live", "alpha"),
        ("live", "work"),
        ("empty", "zzz-empty"),
        ("parked", None),
    ]
